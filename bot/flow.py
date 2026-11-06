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

import re
import uuid
from collections.abc import Iterable, Mapping
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
    #: A question's model answer, shown and played once the question is behind
    #: the learner — passed, or out of tries — and never before (D-132).
    answer_text: str = ""
    answer_audio_url: str | None = None
    #: A drill's sentence and its Khmer meaning, kept apart from ``text`` so a
    #: masked round can hide the one and keep the other as the cue (D-135).
    target_text: str = ""
    hint: str = ""

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
    #: A review carries its own steps (D-133). It is not a lesson the API can be
    #: asked for again, and re-reading the queue mid-review would reshuffle it:
    #: every attempt moves a concept's due date.
    review_steps: tuple[Step, ...] = ()
    #: Which masked round of the current drill the learner is on; 0 is the
    #: sentence in full (D-135). Starts again at 0 on every step.
    mask_round: int = 0

    @property
    def is_review(self) -> bool:
        return bool(self.review_steps)

    def at(self, index: int) -> Session:
        return replace(self, step_index=index, attempts_used=0, mask_round=0)


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


def start_review(steps: tuple[Step, ...]) -> Turn:
    """Open a review of what the API said is due (PRD 3.1 Smart Review).

    The review's id is derived from its steps rather than drawn at random, so
    this module stays free of anything that varies between runs.
    """
    if not steps:
        return Turn(replies=(Reply("bot.review_nothing_due"),), session=None)
    review_id = uuid.uuid5(uuid.NAMESPACE_URL, "review:" + ",".join(str(s.item_id) for s in steps))
    session = Session(lesson_id=review_id, review_steps=steps)
    opening = Reply("bot.review_started", {"count": len(steps)})
    return _present(review_lesson(session), session, preface=(opening,))


def review_lesson(session: Session) -> Lesson:
    """The review a session is part-way through, as the lesson the flow walks."""
    return Lesson(lesson_id=session.lesson_id, title="", steps=session.review_steps)


def resume(lesson: Lesson, session: Session, *, masked_rounds: int = 0) -> Turn:
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
    return _present(lesson, session, preface=(preface,), masked_rounds=masked_rounds)


#: Around the characters a learner should look at again. Brackets rather than
#: words, so the line needs no translation: it is the sentence itself (D-134).
WEAK_OPEN: Final = "【"
WEAK_CLOSE: Final = "】"


#: A word the learner said that the sentence does not have (Azure's miscue type).
INSERTION: Final = "Insertion"


def sentence_words(words: Iterable[Mapping[str, object]]) -> tuple[tuple[str, bool], ...]:
    """The API's word list as (word, weak), without the words the learner added.

    An insertion is not in the sentence, so it cannot be aligned with it — and
    when the same character comes later in the sentence, looking it up would
    jump there and strand every word in between. Said "里 在杯子里" for
    水在杯子里, the first 里 matched the last one and nothing was marked, though
    水 was omitted and 杯子 mispronounced (Q10, D-146).
    """
    return tuple(
        (str(word["word"]), bool(word["weak"]))
        for word in words
        if word.get("error_type") != INSERTION
    )


def mark_weak(reference: str, words: tuple[tuple[str, bool], ...]) -> str | None:
    """The reference sentence with its weak words bracketed, or None if none are.

    ``words`` is the alignment of the sentence's own words, in order: (word,
    weak) — ``sentence_words`` takes the learner's insertions out first. Each is
    looked up from where the previous one ended, so a repeated character is
    marked where it was said. A word still not found — the provider tokenising
    differently from the text — is skipped rather than guessed at. Neighbouring
    weak words share one bracket: 【桌子】, not 【桌】【子】.
    """
    pieces: list[str] = []
    position = 0
    found_weak = False
    for word, weak in words:
        at = reference.find(word, position) if word else -1
        if at < 0:
            continue
        pieces.append(reference[position:at])
        pieces.append(f"{WEAK_OPEN}{word}{WEAK_CLOSE}" if weak else word)
        found_weak = found_weak or weak
        position = at + len(word)
    if not found_weak:
        return None
    pieces.append(reference[position:])
    return "".join(pieces).replace(WEAK_CLOSE + WEAK_OPEN, "")


#: Item types practised again with words hidden once said in full: PRD 3.1's
#: "跟读→逐步遮词". Vocab items are a word or two already, and a question has
#: its own form.
MASKED_ITEM_TYPES: Final[frozenset[str]] = frozenset({"drill"})

#: What stands in for a hidden character. Written as an escape because the
#: full-width low line looks like an ASCII underscore in most editors.
MASK: Final = "\uff3f"

_HAN: Final = re.compile(r"[\u4e00-\u9fff]")


def mask(sentence: str, *, round_number: int, rounds: int) -> str:
    """The sentence with ``round_number / rounds`` of its characters hidden.

    Only Han characters are hidden; punctuation stays, so the shape of the
    sentence is still there to lean on. The last round hides every character —
    the learner says it from memory. Which ones go first is fixed, not random,
    so a learner who retries sees the same gaps and a test can say what they are:
    the k-th character is hidden once ``k % rounds < round_number``.
    """
    if rounds <= 0 or round_number <= 0:
        return sentence
    pieces: list[str] = []
    k = 0
    for char in sentence:
        if _HAN.match(char):
            pieces.append(MASK if k % rounds < round_number else char)
            k += 1
        else:
            pieces.append(char)
    return "".join(pieces)


def on_voice_scored(
    lesson: Lesson,
    session: Session,
    *,
    passed: bool,
    score: float,
    max_retry: int,
    marked: str | None = None,
    masked_rounds: int = 0,
) -> Turn:
    """React to a scored recording, and either retry the step or move on.

    Args:
        passed: the API's verdict. Never recomputed here from ``score`` — that
            comparison belongs to one place (services/mastery/delta.py), and a
            second copy of it is a second thing to keep in step with the pass
            mark M0-1 will move.
        max_retry: SCORING_MAX_RETRY. Attempts *after* the first, so 3 means a
            learner may send four recordings for one sentence.
        marked: the sentence with its weak words bracketed (``mark_weak``),
            shown right after the score; None when there is nothing to point at.
        masked_rounds: DRILL_MASKED_ROUNDS. A drill passed in full, or in any
            round short of the last, is asked for again with more hidden (D-135).
    """
    used = session.attempts_used + 1
    rendered_score = round(score)
    notes = (Reply("bot.weak_words", {"text": marked}),) if marked else ()
    step = lesson.steps[session.step_index]

    if passed:
        verdict = (Reply("bot.score_passed", {"score": rendered_score}), *notes)
        if step.item_type in MASKED_ITEM_TYPES and session.mask_round < masked_rounds:
            harder = replace(session, attempts_used=0, mask_round=session.mask_round + 1)
            return Turn(
                replies=(*verdict, _prompt_for(step, harder.mask_round, masked_rounds)),
                session=harder,
                awaiting_voice=True,
            )
        return _advance(lesson, session, verdict)

    if used > max_retry:
        # Out of tries. The concept keeps the mastery it earned and comes back
        # through the review queue; nothing here decides that. A learner who
        # could not recall a masked sentence is shown it before moving on.
        verdict = (Reply("bot.score_failed_moving_on", {"score": rendered_score}), *notes)
        if session.mask_round > 0 and step.target_text:
            verdict += (
                Reply("bot.model_answer", {"text": step.target_text}, audio_url=step.audio_url),
            )
        return _advance(lesson, session, verdict)

    return Turn(
        replies=(
            Reply(
                "bot.score_failed_retry",
                {"score": rendered_score, "used": used, "limit": max_retry + 1},
            ),
            *notes,
            _prompt_for(step, session.mask_round, masked_rounds),
        ),
        session=replace(session, attempts_used=used),
        awaiting_voice=True,
    )


def _advance(lesson: Lesson, session: Session, verdict: tuple[Reply, ...]) -> Turn:
    """Move past the current step, saying ``verdict`` and the model answer on the way out."""
    step = lesson.steps[session.step_index]
    preface = verdict
    if step.answer_text:
        # Content, not copy: the key renders the answer and nothing else.
        preface += (
            Reply("bot.model_answer", {"text": step.answer_text}, audio_url=step.answer_audio_url),
        )
    next_index = session.step_index + 1
    if next_index >= len(lesson.steps):
        return Turn(replies=preface, session=None, finished=True)
    return _present(lesson, session.at(next_index), preface=preface)


def _present(
    lesson: Lesson, session: Session, *, preface: tuple[Reply, ...], masked_rounds: int = 0
) -> Turn:
    """Show the session's current step, skipping past anything unspeakable.

    Explain cards are read and then left behind in the same turn: there is
    nothing to say back to them (PRD 9.2 gives them no mastery weight), so
    stopping to wait would strand the conversation.
    """
    replies = list(preface)
    index = session.step_index

    while index < len(lesson.steps):
        step = lesson.steps[index]
        # Resuming mid-drill keeps the round the learner reached.
        round_number = session.mask_round if index == session.step_index else 0
        replies.append(_prompt_for(step, round_number, masked_rounds))
        if step.disclaimer_key is not None:
            # PRD 7.4: the avatar is labelled wherever it appears.
            replies.append(Reply(step.disclaimer_key))
        if step.expects_voice:
            return Turn(
                replies=tuple(replies),
                session=replace(session.at(index), mask_round=round_number),
                awaiting_voice=True,
            )
        index += 1

    return Turn(replies=tuple(replies), session=None, finished=True)


def _prompt_for(step: Step, round_number: int = 0, rounds: int = 0) -> Reply:
    """The message for one step, with its recording attached if it has one.

    A masked round shows the sentence with gaps and its Khmer meaning as the
    cue — no pinyin and no recording, which would give the hidden words away.
    """
    if round_number > 0 and step.target_text:
        masked = mask(step.target_text, round_number=round_number, rounds=rounds)
        text = "\n".join(line for line in (masked, step.hint) if line)
        return Reply(PROMPT_KEYS.get(step.item_type, "bot.explain"), {"text": text})
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
    "mark_weak",
    "on_voice_scored",
    "resume",
    "sentence_words",
    "start",
]
