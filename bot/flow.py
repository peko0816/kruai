"""Where a learner is in a lesson, and what the bot says next.

PRD 3.3's runtime: explain -> drill -> vocab -> Q&A -> complete. The order is
not encoded here — it is the order of ``lesson_items.sequence``, which the
content pack decides. This module walks that list, knows which steps need a
recording, and decides whether a failed attempt is retried or left behind.

**Pure.** No Telegram, no HTTP, no Redis, no clock. Everything it needs arrives
as values and everything it produces is a value, which is what lets the whole
conversation be tested without a network (and what keeps the entry layer from
quietly acquiring business logic — ARCHITECTURE section 1).

Two things it deliberately does not decide:

  · whether an attempt passed. The API says so; ``passed`` arrives as an input.
  · how many attempts a step is worth. That is SCORING_MAX_RETRY, read from the
    same configuration the backend reads, and passed in. The bot does not
    invent the number, and there is one number rather than two.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from typing import Final, Literal

#: Item types that expect a voice message back (PRD 3.3 steps 2-4).
SPOKEN_ITEM_TYPES: Final[frozenset[str]] = frozenset({"drill", "vocab", "qa"})

#: The message key for each kind of step. Keys, never sentences.
PROMPT_KEYS: Final[dict[str, str]] = {
    "explain": "bot.explain",
    "drill": "bot.prompt_drill",
    "vocab": "bot.prompt_vocab",
    "qa": "bot.prompt_qa",
}

#: What to do with a step the learner just failed.
Outcome = Literal["advance", "retry"]


@dataclass(frozen=True)
class Step:
    """One lesson item, as far as the conversation cares."""

    item_id: uuid.UUID
    item_type: str
    #: The sentence to say, or the explanation to read. From the content pack.
    text: str
    #: Chosen by the server (D2); the bot plays it and never decides it.
    audio_url: str | None = None
    video_url: str | None = None
    #: Set by the server only when video is being served (PRD 7.4).
    disclaimer_key: str | None = None

    @property
    def expects_voice(self) -> bool:
        return self.item_type in SPOKEN_ITEM_TYPES


@dataclass(frozen=True)
class Lesson:
    """The unit being worked through."""

    lesson_id: uuid.UUID
    title: str
    steps: tuple[Step, ...]


@dataclass(frozen=True)
class Session:
    """How far through a lesson one learner is.

    ``attempts_used`` counts recordings sent for the *current* step only, and
    resets when the conversation moves on. A learner who struggles with one
    sentence has not used up their patience for the next one.
    """

    lesson_id: uuid.UUID
    step_index: int = 0
    attempts_used: int = 0

    def at(self, index: int) -> Session:
        return replace(self, step_index=index, attempts_used=0)


@dataclass(frozen=True)
class Reply:
    """One thing to send. A key and its parameters, never a sentence.

    Rendering happens at the Telegram boundary so that every string on screen
    comes from locales/ (CLAUDE.md section 7) and so these can be asserted in
    tests without matching prose.
    """

    key: str
    params: dict[str, object] | None = None
    #: A recording to send alongside the text, if the step has one.
    audio_url: str | None = None


@dataclass(frozen=True)
class Turn:
    """What the bot says, and where the learner ends up.

    ``session`` is None when the lesson is over — the caller forgets them and
    calls the completion endpoint.
    """

    replies: tuple[Reply, ...]
    session: Session | None
    #: True when this turn ended the lesson, so the caller knows to complete it.
    finished: bool = False
    #: True when the next thing expected from the learner is a recording.
    awaiting_voice: bool = False


def start(lesson: Lesson) -> Turn:
    """Open a lesson at its first step."""
    if not lesson.steps:
        return Turn(replies=(Reply("bot.no_content"),), session=None, finished=False)

    session = Session(lesson_id=lesson.lesson_id)
    opening = Reply(
        "bot.lesson_started",
        {"title": lesson.title, "count": len(lesson.steps)},
    )
    return _present(lesson, session, preface=(opening,))


def resume(lesson: Lesson, session: Session) -> Turn:
    """Pick a lesson back up where the learner left it.

    A step index past the end means the lesson grew shorter under them — a pack
    was reimported mid-session. Treated as finished rather than as an error:
    they did the lesson that existed when they started it.
    """
    if session.step_index >= len(lesson.steps):
        return Turn(replies=(), session=None, finished=True)

    preface = Reply(
        "bot.lesson_resumed",
        {
            "title": lesson.title,
            "step": session.step_index + 1,
            "count": len(lesson.steps),
        },
    )
    return _present(lesson, session, preface=(preface,))


def on_voice_scored(
    lesson: Lesson, session: Session, *, passed: bool, score: float, max_retry: int
) -> Turn:
    """React to a scored recording, and either retry the step or move on.

    Args:
        passed: the API's verdict. Never recomputed here from ``score`` — that
            comparison belongs to one place (services/mastery/delta.py), and a
            second copy of it is a second thing to keep in step with the pass
            mark M0-1 will move.
        max_retry: SCORING_MAX_RETRY. Attempts *after* the first, so 3 means a
            learner may send four recordings for one sentence.
    """
    used = session.attempts_used + 1
    rendered_score = round(score)

    if passed:
        return _advance(lesson, session, Reply("bot.score_passed", {"score": rendered_score}))

    if used > max_retry:
        # Out of tries. The concept keeps the mastery it earned and comes back
        # through the review queue; nothing here decides that.
        return _advance(
            lesson,
            session,
            Reply("bot.score_failed_moving_on", {"score": rendered_score}),
        )

    step = lesson.steps[session.step_index]
    return Turn(
        replies=(
            Reply(
                "bot.score_failed_retry",
                {"score": rendered_score, "used": used, "limit": max_retry + 1},
            ),
            _prompt_for(step),
        ),
        session=replace(session, attempts_used=used),
        awaiting_voice=True,
    )


def _advance(lesson: Lesson, session: Session, verdict: Reply) -> Turn:
    """Move past the current step, saying ``verdict`` on the way out."""
    next_index = session.step_index + 1
    if next_index >= len(lesson.steps):
        return Turn(replies=(verdict,), session=None, finished=True)
    return _present(lesson, session.at(next_index), preface=(verdict,))


def _present(lesson: Lesson, session: Session, *, preface: tuple[Reply, ...]) -> Turn:
    """Show the session's current step, skipping past anything unspeakable.

    Explain cards are read and then left behind in the same turn: there is
    nothing to say back to them (PRD 9.2 gives them no mastery weight), so
    stopping to wait would strand the conversation.
    """
    replies = list(preface)
    index = session.step_index

    while index < len(lesson.steps):
        step = lesson.steps[index]
        replies.append(_prompt_for(step))
        if step.disclaimer_key is not None:
            # PRD 7.4: the avatar is labelled wherever it appears.
            replies.append(Reply(step.disclaimer_key))
        if step.expects_voice:
            return Turn(
                replies=tuple(replies),
                session=session.at(index),
                awaiting_voice=True,
            )
        index += 1

    return Turn(replies=tuple(replies), session=None, finished=True)


def _prompt_for(step: Step) -> Reply:
    """The message for one step, with its recording attached if it has one."""
    return Reply(
        PROMPT_KEYS.get(step.item_type, "bot.explain"),
        {"text": step.text},
        audio_url=step.audio_url,
    )


__all__ = [
    "PROMPT_KEYS",
    "SPOKEN_ITEM_TYPES",
    "Lesson",
    "Outcome",
    "Reply",
    "Session",
    "Step",
    "Turn",
    "on_voice_scored",
    "resume",
    "start",
]
