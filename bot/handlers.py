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

import dataclasses
from dataclasses import dataclass
from typing import Any, Final

from telegram import Update
from telegram.ext import ContextTypes

from app.core.logging import get_logger
from app.core.security import normalize_locale
from bot import i18n
from bot.api_client import ApiError, KruaiApi
from bot.audio import AudioSender
from bot.flow import (
    Lesson,
    Reply,
    Turn,
    mark_weak,
    on_voice_scored,
    resume,
    review_lesson,
    sentence_words,
    start,
    start_review,
)
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
    "payment.unavailable": "bot.payments_unavailable",
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

    def __init__(
        self, api: KruaiApi, sessions: SessionStore, *, max_retry: int, masked_rounds: int = 0
    ) -> None:
        self._api = api
        self._sessions = sessions
        self._max_retry = max_retry
        #: DRILL_MASKED_ROUNDS (D-135); 0 keeps every drill to one round in full.
        self._masked_rounds = masked_rounds

    async def on_start(self, learner: Learner) -> list[Outgoing]:
        learner = await self._in_their_language(learner)
        return _render(
            [
                Reply("bot.welcome"),
                Reply("bot.help"),
                Reply("bot.help_review"),
                Reply("bot.help_language"),
            ],
            learner.locale,
        )

    async def on_language_menu(self, learner: Learner) -> list[Outgoing]:
        """The three interface languages, each a command to tap (Q12, D-148)."""
        learner = await self._in_their_language(learner)
        return _render([Reply("bot.language_menu")], learner.locale)

    async def on_language(self, learner: Learner, locale: str) -> list[Outgoing]:
        """Switch the interface language, stored on the account, and say so in it."""
        try:
            token = await self._token(learner)
            chosen = await self._api.set_locale(token, learner.telegram_id, locale)
        except ApiError as exc:
            return self._on_api_error(exc, await self._in_their_language(learner))
        return _render([Reply("bot.language_set")], chosen)

    async def on_status(self, learner: Learner) -> list[Outgoing]:
        """What is left of today, in the learner's own words."""
        learner = await self._in_their_language(learner)
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
        learner = await self._in_their_language(learner)
        try:
            token = await self._token(learner)
            stored = await self._sessions.load(learner.telegram_id)
            if stored is not None and stored.is_review:
                # /learn means a lesson. The review is put down; what it
                # scored is already recorded.
                await self._sessions.clear(learner.telegram_id)
                stored = None
            if stored is not None:
                lesson = await self._api.lesson(token, stored.lesson_id)
                turn = resume(lesson, stored, masked_rounds=self._masked_rounds)
                return await self._play(learner, lesson, turn, token)

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

    async def on_review(self, learner: Learner) -> list[Outgoing]:
        """Practise what is due (D-133). Replaces whatever was open: a lesson's
        progress is the server's, and /learn picks it up again."""
        learner = await self._in_their_language(learner)
        try:
            token = await self._token(learner)
            steps = await self._api.review_steps(token)
        except ApiError as exc:
            return self._on_api_error(exc, learner)
        turn = start_review(steps)
        if turn.session is None:
            return _render(list(turn.replies), learner.locale)
        return await self._play(learner, review_lesson(turn.session), turn, token, review=True)

    async def on_stop(self, learner: Learner) -> list[Outgoing]:
        """Put the lesson down. Progress already recorded stays recorded."""
        learner = await self._in_their_language(learner)
        await self._sessions.clear(learner.telegram_id)
        return _render([Reply("bot.lesson_stopped")], learner.locale)

    async def on_voice(
        self, learner: Learner, *, audio: bytes, filename: str = "voice.ogg"
    ) -> list[Outgoing]:
        """Score one recording against the step the learner is on."""
        learner = await self._in_their_language(learner)
        session = await self._sessions.load(learner.telegram_id)
        if session is None:
            return _render([Reply("bot.not_in_lesson")], learner.locale)

        try:
            token = await self._token(learner)
            lesson = (
                review_lesson(session)
                if session.is_review
                else await self._api.lesson(token, session.lesson_id)
            )
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
            masked_rounds=self._masked_rounds,
            marked=mark_weak(
                result.get("reference_text") or "", sentence_words(result.get("words", ()))
            ),
        )
        return await self._play(learner, lesson, turn, token, review=session.is_review)

    # ----------------------------------------------------------- internals

    async def _play(
        self, learner: Learner, lesson: Lesson, turn: Turn, token: str, *, review: bool = False
    ) -> list[Outgoing]:
        """Send a turn's replies and store where it left the learner."""
        replies = list(turn.replies)

        if turn.session is not None:
            await self._sessions.save(learner.telegram_id, turn.session)
        else:
            await self._sessions.clear(learner.telegram_id)

        if turn.finished:
            # A review is not a lesson: there is nothing to complete, and the
            # attempts have already moved every concept it touched.
            if review:
                replies.append(Reply("bot.review_finished"))
            else:
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

    async def _in_their_language(self, learner: Learner) -> Learner:
        """The learner, speaking the language stored on their account.

        Signing in returns it, and the token cache keeps it, so this costs a
        request once per token, not per message. If the API cannot be reached
        the first-contact guess stands; the call that follows reports the
        failure in it.
        """
        try:
            await self._token(learner)
        except ApiError:
            return learner
        stored = self._api.locale_of(learner.telegram_id)
        return dataclasses.replace(learner, locale=stored) if stored else learner

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
    return Learner(
        telegram_id=user.id,
        # The same first-contact rule the API applies when it creates the
        # account (D-148); the account's stored choice replaces it.
        locale=normalize_locale(language_code),
        language_code=language_code,
    )


# ----------------------------------------------------- python-telegram-bot glue


async def _send(
    update: Update, context: ContextTypes.DEFAULT_TYPE, messages: list[Outgoing]
) -> None:
    message = update.effective_message
    if message is None:  # pragma: no cover - command and voice updates carry one
        return
    audio: AudioSender = context.application.bot_data["audio"]
    for outgoing in messages:
        if outgoing.audio_url:
            # Uploaded, not linked: Telegram takes only OGG by link (Q9, D-143).
            await audio.send(message, text=outgoing.text, audio_url=outgoing.audio_url)
        else:
            await message.reply_text(outgoing.text)


def _conversation(context: ContextTypes.DEFAULT_TYPE) -> Conversation:
    conversation: Conversation = context.application.bot_data["conversation"]
    return conversation


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send(update, context, await _conversation(context).on_start(learner_from(update)))


async def learn_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send(update, context, await _conversation(context).on_learn(learner_from(update)))


async def review_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send(update, context, await _conversation(context).on_review(learner_from(update)))


async def language_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send(
        update, context, await _conversation(context).on_language_menu(learner_from(update))
    )


#: The tappable commands in bot.language_menu, and the locale each one picks.
LANGUAGE_COMMANDS: Final[dict[str, str]] = {"lang_km": "km", "lang_zh": "zh", "lang_en": "en"}


def _set_language_to(locale: str) -> Any:
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await _send(
            update, context, await _conversation(context).on_language(learner_from(update), locale)
        )

    return handler


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send(update, context, await _conversation(context).on_stop(learner_from(update)))


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send(update, context, await _conversation(context).on_status(learner_from(update)))


async def voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Download the voice note and hand its bytes to the conversation."""
    message = update.effective_message
    if message is None or message.voice is None:  # pragma: no cover - filtered upstream
        return
    telegram_file = await message.voice.get_file()
    audio: bytes = bytes(await telegram_file.download_as_bytearray())
    await _send(
        update,
        context,
        await _conversation(context).on_voice(
            learner_from(update), audio=audio, filename=f"{message.voice.file_unique_id}.ogg"
        ),
    )


def register(application: Any, conversation: Conversation, audio: AudioSender) -> None:
    """Attach the handlers to a python-telegram-bot application."""
    from telegram.ext import CommandHandler, MessageHandler, filters

    application.bot_data["conversation"] = conversation
    application.bot_data["audio"] = audio
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("learn", learn_command))
    application.add_handler(CommandHandler("review", review_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("language", language_command))
    for command, locale in LANGUAGE_COMMANDS.items():
        application.add_handler(CommandHandler(command, _set_language_to(locale)))
    application.add_handler(MessageHandler(filters.VOICE, voice_message))


__all__ = ["Conversation", "Learner", "Outgoing", "learner_from", "register"]
