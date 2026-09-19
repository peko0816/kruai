"""Telegram in, Telegram out. Everything between is a call or a pure function.

The conversation logic is bot/flow.py and the decisions are the API's. What is
here is the wiring: read the update, fetch what the flow needs, send what the
flow produced, and remember where the learner ended up.

Handlers are split in two on purpose. ``Conversation`` holds the behaviour and
takes plain values — a learner id, a locale, some audio bytes — so the whole
lesson can be played through in a test with no Telegram at all. The functions at
the bottom are the adapters python-telegram-bot calls, and they contain nothing
worth testing beyond "it passed the right arguments along".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from telegram import Update
from telegram.ext import ContextTypes

from app.core.logging import get_logger
from bot import i18n
from bot.api_client import ApiError, KruaiApi
from bot.flow import Lesson, Reply, Turn, on_voice_scored, resume, start
from bot.session import SessionStore

log = get_logger(__name__)

#: Error codes the API can answer with that the learner is told about in their
#: own words. Anything else becomes the generic failure message — a learner
#: should never be shown an internal code.
#:
#: Named ``_KEYS`` because its values are message keys, which is the convention
#: bot/i18n_check.py uses to find them: an error code and a message key are the
#: same shape and only one of them lives in locales/.
_ERROR_MESSAGE_KEYS: Final[dict[str, str]] = {
    "quota.insufficient": "bot.quota_exhausted",
    "payment.already_subscribed": "bot.already_subscribed",
    "scoring.unavailable": "bot.scoring_unavailable",
    "cost.cap_reached": "bot.cost_cap_reached",
    "attempt.item_not_scorable": "bot.error",
    "attempt.audio_too_large": "bot.error",
}


@dataclass(frozen=True)
class Learner:
    """Who sent the message, as far as the conversation cares."""

    telegram_id: int
    locale: str
    language_code: str | None = None


@dataclass(frozen=True)
class Outgoing:
    """A rendered message, ready for whatever sends it."""

    text: str
    audio_url: str | None = None


class Conversation:
    """One learner's side of the bot, with no Telegram types in sight."""

    def __init__(self, api: KruaiApi, sessions: SessionStore, *, max_retry: int) -> None:
        self._api = api
        self._sessions = sessions
        self._max_retry = max_retry

    async def on_start(self, learner: Learner) -> list[Outgoing]:
        return _render([Reply("bot.welcome"), Reply("bot.help")], learner.locale)

    async def on_status(self, learner: Learner) -> list[Outgoing]:
        """What is left of today, in the learner's own words."""
        try:
            token = await self._token(learner)
            entitlements = await self._api.entitlements(token)
        except ApiError as exc:
            return self._on_api_error(exc, learner)

        attempts = entitlements["attempts"]
        limit = attempts["limit"]
        suffix = (
            i18n.t("bot.status_unlimited", locale=learner.locale)
            if limit is None
            else i18n.t("bot.status_limit_suffix", locale=learner.locale, limit=limit)
        )
        return _render(
            [
                Reply(
                    "bot.status",
                    {
                        "plan": entitlements["plan"],
                        "used": attempts["used"],
                        "limit_suffix": suffix,
                    },
                )
            ],
            learner.locale,
        )

    async def on_learn(self, learner: Learner) -> list[Outgoing]:
        """Start a lesson, or carry on with the one already open."""
        try:
            token = await self._token(learner)
            stored = await self._sessions.load(learner.telegram_id)
            if stored is not None:
                lesson = await self._api.lesson(token, stored.lesson_id)
                return await self._play(learner, lesson, resume(lesson, stored), token)

            lesson_id = await self._api.next_lesson_id(token)
            if lesson_id is None:
                # Nothing left to do is a different thing from nothing to do.
                # Written as two Reply() calls rather than one with a chosen
                # key, so bot/i18n_check.py can see both — it recognises keys
                # by position, and a key assembled into a variable is invisible
                # to it (which is how this one was found).
                if await self._api.has_courses(token):
                    return _render([Reply("bot.all_lessons_done")], learner.locale)
                return _render([Reply("bot.no_content")], learner.locale)
            # Starting is what costs a task (PRD 4.3), so a learner who is out
            # of them is refused here, before any of the lesson is shown.
            await self._api.start_lesson(token, lesson_id)
            lesson = await self._api.lesson(token, lesson_id)
            return await self._play(learner, lesson, start(lesson), token)
        except ApiError as exc:
            return self._on_api_error(exc, learner)

    async def on_stop(self, learner: Learner) -> list[Outgoing]:
        """Put the lesson down. Progress already recorded stays recorded."""
        await self._sessions.clear(learner.telegram_id)
        return _render([Reply("bot.lesson_stopped")], learner.locale)

    async def on_voice(
        self, learner: Learner, *, audio: bytes, filename: str = "voice.ogg"
    ) -> list[Outgoing]:
        """Score one recording against the step the learner is on."""
        session = await self._sessions.load(learner.telegram_id)
        if session is None:
            return _render([Reply("bot.not_in_lesson")], learner.locale)

        try:
            token = await self._token(learner)
            lesson = await self._api.lesson(token, session.lesson_id)
            if session.step_index >= len(lesson.steps):
                # The pack shrank under them; resume() handles this shape.
                return await self._play(learner, lesson, resume(lesson, session), token)

            step = lesson.steps[session.step_index]
            if not step.expects_voice:
                return _render([Reply("bot.awaiting_voice")], learner.locale)

            result = await self._api.submit_attempt(
                token, lesson_item_id=step.item_id, audio=audio, filename=filename
            )
        except ApiError as exc:
            return self._on_api_error(exc, learner)

        turn = on_voice_scored(
            lesson,
            session,
            passed=bool(result["passed"]),
            score=float(result["scores"]["pron"] or 0.0),
            max_retry=self._max_retry,
        )
        return await self._play(learner, lesson, turn, token)

    # ----------------------------------------------------------- internals

    async def _play(
        self, learner: Learner, lesson: Lesson, turn: Turn, token: str
    ) -> list[Outgoing]:
        """Send a turn's replies and store where it left the learner."""
        replies = list(turn.replies)

        if turn.session is not None:
            await self._sessions.save(learner.telegram_id, turn.session)
        else:
            await self._sessions.clear(learner.telegram_id)

        if turn.finished:
            replies.append(await self._complete(lesson, token))

        log.info(
            "bot.turn",
            telegram_id=learner.telegram_id,
            lesson_id=str(lesson.lesson_id),
            step=None if turn.session is None else turn.session.step_index,
            finished=turn.finished,
            awaiting_voice=turn.awaiting_voice,
        )
        return _render(replies, learner.locale)

    async def _complete(self, lesson: Lesson, token: str) -> Reply:
        """Mark the lesson done and say what it queued for review."""
        body = await self._api.complete_lesson(token, lesson.lesson_id)
        return Reply(
            "bot.lesson_finished",
            {"scheduled": body.get("concepts_tracked", 0)},
        )

    async def _token(self, learner: Learner) -> str:
        return await self._api.token_for(learner.telegram_id, language_code=learner.language_code)

    def _on_api_error(self, exc: ApiError, learner: Learner) -> list[Outgoing]:
        """Turn an API refusal into something a learner can act on.

        An unrecognised code becomes the generic message rather than leaking a
        machine-readable string onto someone's screen. The code itself goes to
        the log, where it is useful.
        """
        if exc.status == 401:
            # The cached token was rejected; drop it so the next message
            # re-authenticates rather than repeating the same failure.
            self._api.forget(learner.telegram_id)

        key = _ERROR_MESSAGE_KEYS.get(exc.code or "", "bot.error")
        log.warning(
            "bot.api_error",
            telegram_id=learner.telegram_id,
            status=exc.status,
            error_code=exc.code,
            reply_key=key,
        )
        params: dict[str, object] = {"resets_at": ""} if key == "bot.quota_exhausted" else {}
        return _render([Reply(key, params or None)], learner.locale)


def _render(replies: list[Reply], locale: str) -> list[Outgoing]:
    """Turn keys into sentences, at the last possible moment."""
    return [
        Outgoing(
            text=i18n.t(reply.key, locale=locale, **(reply.params or {})),
            audio_url=reply.audio_url,
        )
        for reply in replies
    ]


def learner_from(update: Update) -> Learner:
    """The sender, with their Telegram language mapped onto a shipped locale."""
    user = update.effective_user
    if user is None:  # pragma: no cover - Telegram always sends one for these updates
        raise ValueError("update has no sender")
    language_code = user.language_code
    primary = (language_code or "").split("-")[0].lower()
    return Learner(
        telegram_id=user.id,
        locale=i18n.normalise_locale(primary),
        language_code=language_code,
    )


# ----------------------------------------------------- python-telegram-bot glue


async def _send(update: Update, messages: list[Outgoing]) -> None:
    message = update.effective_message
    if message is None:  # pragma: no cover - command and voice updates carry one
        return
    for outgoing in messages:
        if outgoing.audio_url:
            await message.reply_voice(voice=outgoing.audio_url, caption=outgoing.text)
        else:
            await message.reply_text(outgoing.text)


def _conversation(context: ContextTypes.DEFAULT_TYPE) -> Conversation:
    conversation: Conversation = context.application.bot_data["conversation"]
    return conversation


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send(update, await _conversation(context).on_start(learner_from(update)))


async def learn_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send(update, await _conversation(context).on_learn(learner_from(update)))


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send(update, await _conversation(context).on_stop(learner_from(update)))


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send(update, await _conversation(context).on_status(learner_from(update)))


async def voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Download the voice note and hand its bytes to the conversation."""
    message = update.effective_message
    if message is None or message.voice is None:  # pragma: no cover - filtered upstream
        return
    telegram_file = await message.voice.get_file()
    audio: bytes = bytes(await telegram_file.download_as_bytearray())
    await _send(
        update,
        await _conversation(context).on_voice(
            learner_from(update), audio=audio, filename=f"{message.voice.file_unique_id}.ogg"
        ),
    )


def register(application: Any, conversation: Conversation) -> None:
    """Attach the handlers to a python-telegram-bot application."""
    from telegram.ext import CommandHandler, MessageHandler, filters

    application.bot_data["conversation"] = conversation
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("learn", learn_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(MessageHandler(filters.VOICE, voice_message))


__all__ = ["Conversation", "Learner", "Outgoing", "learner_from", "register"]
