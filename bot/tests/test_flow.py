"""The lesson state machine, with no Telegram, no HTTP and no database.

PRD 3.3's sequence is explain -> drill -> vocab -> Q&A -> complete, and the
cases worth writing down are the ones where it does not go straight through:
a card with nothing to say back to it, a sentence someone cannot get right, a
pack that changed under a learner mid-lesson.
"""

from __future__ import annotations

import uuid

import pytest

from bot.flow import Lesson, Session, Step, on_voice_scored, resume, start

MAX_RETRY = 3


def step(item_type: str, text: str = "我要水", **kwargs: object) -> Step:
    return Step(item_id=uuid.uuid4(), item_type=item_type, text=text, **kwargs)  # type: ignore[arg-type]


def lesson(*types: str) -> Lesson:
    return Lesson(
        lesson_id=uuid.uuid4(),
        title="មេរៀនទី ១",
        steps=tuple(step(item_type) for item_type in types),
    )


def keys(turn: object) -> list[str]:
    return [reply.key for reply in turn.replies]  # type: ignore[attr-defined]


# ------------------------------------------------------------------- starting


def test_a_lesson_opens_on_its_first_spoken_step() -> None:
    """The explain card is read on the way past, not waited on."""
    unit = lesson("explain", "drill", "vocab", "qa")

    turn = start(unit)

    assert keys(turn) == ["bot.lesson_started", "bot.explain", "bot.prompt_drill"]
    assert turn.awaiting_voice is True
    assert turn.session is not None
    assert turn.session.step_index == 1


def test_several_explain_cards_in_a_row_are_all_read() -> None:
    unit = lesson("explain", "explain", "drill")

    turn = start(unit)

    assert keys(turn).count("bot.explain") == 2
    assert turn.session is not None and turn.session.step_index == 2


def test_a_lesson_of_only_explain_cards_finishes_immediately() -> None:
    """Nothing to say back, so stopping to wait would strand the learner."""
    turn = start(lesson("explain", "explain"))

    assert turn.finished is True
    assert turn.session is None
    assert turn.awaiting_voice is False


def test_an_empty_lesson_says_so_rather_than_starting() -> None:
    turn = start(Lesson(lesson_id=uuid.uuid4(), title="empty", steps=()))

    assert keys(turn) == ["bot.no_content"]
    assert turn.session is None


def test_the_video_disclaimer_rides_along_when_the_server_sent_one() -> None:
    """PRD 7.4 is a hard constraint: the avatar is labelled wherever it appears."""
    unit = Lesson(
        lesson_id=uuid.uuid4(),
        title="t",
        steps=(step("explain", disclaimer_key="avatar.ai_disclaimer"), step("drill")),
    )

    assert "avatar.ai_disclaimer" in keys(start(unit))


def test_no_disclaimer_when_the_server_did_not_send_one() -> None:
    assert "avatar.ai_disclaimer" not in keys(start(lesson("explain", "drill")))


# ------------------------------------------------------------------ scoring


def test_a_struggle_on_one_step_does_not_shorten_the_next() -> None:
    """The retry budget is per step.

    Without this, a learner who needed three goes at one sentence arrives at
    the next one with no tries left — and the bot walks them through the rest
    of the lesson without ever letting them try twice. Nothing errors; they
    just quietly stop being allowed to practise.
    """
    unit = lesson("drill", "vocab", "qa")
    struggled = Session(lesson_id=unit.lesson_id, step_index=0, attempts_used=2)

    moved_on = on_voice_scored(unit, struggled, passed=True, score=85.0, max_retry=MAX_RETRY)
    assert moved_on.session is not None
    assert moved_on.session.attempts_used == 0

    # And the fresh budget is really usable: a failure here is a retry, not a
    # "moving on", which is what the carried count would have produced.
    failed_next = on_voice_scored(
        unit, moved_on.session, passed=False, score=20.0, max_retry=MAX_RETRY
    )
    assert keys(failed_next)[0] == "bot.score_failed_retry"


def test_passing_moves_to_the_next_step() -> None:
    unit = lesson("drill", "vocab")
    session = Session(lesson_id=unit.lesson_id, step_index=0)

    turn = on_voice_scored(unit, session, passed=True, score=88.0, max_retry=MAX_RETRY)

    assert keys(turn) == ["bot.score_passed", "bot.prompt_vocab"]
    assert turn.session is not None and turn.session.step_index == 1
    assert turn.session.attempts_used == 0, "a fresh step starts with a fresh count"


def test_failing_asks_for_the_same_step_again() -> None:
    unit = lesson("drill", "vocab")
    session = Session(lesson_id=unit.lesson_id, step_index=0)

    turn = on_voice_scored(unit, session, passed=False, score=31.0, max_retry=MAX_RETRY)

    assert keys(turn) == ["bot.score_failed_retry", "bot.prompt_drill"]
    assert turn.session is not None and turn.session.step_index == 0
    assert turn.session.attempts_used == 1
    assert turn.awaiting_voice is True


@pytest.mark.parametrize("already_used", [0, 1, 2])
def test_retries_are_offered_up_to_the_configured_limit(already_used: int) -> None:
    unit = lesson("drill", "vocab")
    session = Session(lesson_id=unit.lesson_id, attempts_used=already_used)

    turn = on_voice_scored(unit, session, passed=False, score=20.0, max_retry=MAX_RETRY)

    assert keys(turn)[0] == "bot.score_failed_retry"


def test_the_last_retry_moves_on_rather_than_looping() -> None:
    """PRD 3.3 caps it at SCORING_MAX_RETRY; the concept comes back through
    the review queue instead, which is not this module's decision."""
    unit = lesson("drill", "vocab")
    session = Session(lesson_id=unit.lesson_id, attempts_used=MAX_RETRY)

    turn = on_voice_scored(unit, session, passed=False, score=20.0, max_retry=MAX_RETRY)

    assert keys(turn) == ["bot.score_failed_moving_on", "bot.prompt_vocab"]
    assert turn.session is not None and turn.session.step_index == 1


def test_a_zero_retry_configuration_moves_on_at_once() -> None:
    unit = lesson("drill", "vocab")

    turn = on_voice_scored(
        unit, Session(lesson_id=unit.lesson_id), passed=False, score=5.0, max_retry=0
    )

    assert keys(turn)[0] == "bot.score_failed_moving_on"


def test_passing_the_last_step_finishes_the_lesson() -> None:
    unit = lesson("drill")
    session = Session(lesson_id=unit.lesson_id)

    turn = on_voice_scored(unit, session, passed=True, score=90.0, max_retry=MAX_RETRY)

    assert turn.finished is True
    assert turn.session is None
    assert keys(turn) == ["bot.score_passed"]


def test_running_out_of_retries_on_the_last_step_still_finishes() -> None:
    unit = lesson("drill")
    session = Session(lesson_id=unit.lesson_id, attempts_used=MAX_RETRY)

    turn = on_voice_scored(unit, session, passed=False, score=12.0, max_retry=MAX_RETRY)

    assert turn.finished is True
    assert turn.session is None


def test_the_verdict_never_second_guesses_the_api() -> None:
    """``passed`` is the server's answer. Recomputing it from the score here
    would be a second copy of a threshold M0-1 is going to move."""
    unit = lesson("drill", "vocab")
    session = Session(lesson_id=unit.lesson_id)

    generous = on_voice_scored(unit, session, passed=True, score=3.0, max_retry=MAX_RETRY)
    harsh = on_voice_scored(unit, session, passed=False, score=99.0, max_retry=MAX_RETRY)

    assert keys(generous)[0] == "bot.score_passed"
    assert keys(harsh)[0] == "bot.score_failed_retry"


def test_an_explain_card_after_a_drill_is_read_on_the_way_through() -> None:
    unit = lesson("drill", "explain", "qa")
    session = Session(lesson_id=unit.lesson_id)

    turn = on_voice_scored(unit, session, passed=True, score=91.0, max_retry=MAX_RETRY)

    assert keys(turn) == ["bot.score_passed", "bot.explain", "bot.prompt_qa"]
    assert turn.session is not None and turn.session.step_index == 2


# ------------------------------------------------------------------ resuming


def test_resuming_picks_up_at_the_stored_step() -> None:
    unit = lesson("explain", "drill", "vocab")
    session = Session(lesson_id=unit.lesson_id, step_index=2)

    turn = resume(unit, session)

    assert keys(turn) == ["bot.lesson_resumed", "bot.prompt_vocab"]
    assert turn.replies[0].params == {"title": unit.title, "step": 3, "count": 3}


def test_resuming_past_the_end_finishes_without_announcing_a_step() -> None:
    """A pack reimported mid-session can leave a stored index past the end.

    Finishing quietly is the point: the obvious implementation still finishes,
    but on the way it says "continuing, step 8 of 1" — a sentence that is both
    wrong and impossible to act on.
    """
    unit = lesson("drill")
    session = Session(lesson_id=unit.lesson_id, step_index=7)

    turn = resume(unit, session)

    assert turn.finished is True
    assert turn.session is None
    assert turn.replies == (), "nothing to announce about a step that is not there"


def test_the_recording_travels_with_the_prompt() -> None:
    unit = Lesson(
        lesson_id=uuid.uuid4(),
        title="t",
        steps=(step("drill", audio_url="https://cdn.example/a.mp3"),),
    )

    turn = start(unit)

    assert turn.replies[-1].audio_url == "https://cdn.example/a.mp3"
