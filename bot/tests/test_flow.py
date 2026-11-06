"""The lesson state machine, with no Telegram, no HTTP and no database.

PRD 3.3's sequence is explain -> drill -> vocab -> Q&A -> complete, and the
cases worth writing down are the ones where it does not go straight through:
a card with nothing to say back to it, a sentence someone cannot get right, a
pack that changed under a learner mid-lesson.
"""

from __future__ import annotations

import uuid

import pytest

from bot.flow import (
    MASK,
    Lesson,
    Session,
    Step,
    mark_weak,
    mask,
    on_voice_scored,
    resume,
    review_lesson,
    sentence_words,
    start,
    start_review,
)

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


# --------------------------------------------------------- the model answer (D-132)


def asking(answer: str = "我要水。", audio: str | None = "https://cdn.example/a.mp3") -> Lesson:
    question = step("qa", "你要什么?", answer_text=answer, answer_audio_url=audio)
    return Lesson(lesson_id=uuid.uuid4(), title="t", steps=(question, step("drill")))


def test_the_model_answer_follows_a_passed_question() -> None:
    unit = asking()
    session = Session(lesson_id=unit.lesson_id)

    turn = on_voice_scored(unit, session, passed=True, score=90.0, max_retry=MAX_RETRY)

    assert keys(turn) == ["bot.score_passed", "bot.model_answer", "bot.prompt_drill"]
    answer = turn.replies[1]
    assert answer.params == {"text": "我要水。"}
    assert answer.audio_url == "https://cdn.example/a.mp3"


def test_the_model_answer_follows_a_question_the_learner_ran_out_of_tries_on() -> None:
    """The moment they most need to hear it."""
    unit = asking()
    session = Session(lesson_id=unit.lesson_id, attempts_used=MAX_RETRY)

    turn = on_voice_scored(unit, session, passed=False, score=20.0, max_retry=MAX_RETRY)

    assert keys(turn)[:2] == ["bot.score_failed_moving_on", "bot.model_answer"]


def test_the_model_answer_is_not_given_away_before_a_retry() -> None:
    unit = asking()
    session = Session(lesson_id=unit.lesson_id)

    turn = on_voice_scored(unit, session, passed=False, score=20.0, max_retry=MAX_RETRY)

    assert "bot.model_answer" not in keys(turn)


def test_a_question_on_the_last_step_still_gets_its_answer() -> None:
    question = step("qa", "你要什么?", answer_text="我要水。")
    unit = Lesson(lesson_id=uuid.uuid4(), title="t", steps=(question,))
    session = Session(lesson_id=unit.lesson_id)

    turn = on_voice_scored(unit, session, passed=True, score=90.0, max_retry=MAX_RETRY)

    assert keys(turn) == ["bot.score_passed", "bot.model_answer"]
    assert turn.finished


def test_a_step_without_an_answer_says_nothing_extra() -> None:
    unit = lesson("drill", "vocab")
    session = Session(lesson_id=unit.lesson_id)

    turn = on_voice_scored(unit, session, passed=True, score=90.0, max_retry=MAX_RETRY)

    assert "bot.model_answer" not in keys(turn)


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


# ------------------------------------------------------------------- review (D-133)


def test_an_empty_review_says_nothing_is_due() -> None:
    turn = start_review(())

    assert keys(turn) == ["bot.review_nothing_due"]
    assert turn.session is None


def test_a_review_opens_on_its_first_step_and_carries_its_steps() -> None:
    steps = (step("drill"), step("vocab"))

    turn = start_review(steps)

    assert keys(turn) == ["bot.review_started", "bot.prompt_drill"]
    assert turn.replies[0].params == {"count": 2}
    assert turn.session is not None and turn.session.is_review
    assert review_lesson(turn.session).steps == steps


def test_a_review_s_id_is_the_same_for_the_same_steps() -> None:
    """Pure: no randomness in the flow module."""
    steps = (step("drill"),)

    first, second = start_review(steps).session, start_review(steps).session

    assert first is not None and second is not None
    assert first.lesson_id == second.lesson_id


def test_moving_through_a_review_keeps_it_a_review() -> None:
    turn = start_review((step("drill"), step("vocab")))
    assert turn.session is not None
    unit = review_lesson(turn.session)

    after = on_voice_scored(unit, turn.session, passed=True, score=90.0, max_retry=MAX_RETRY)

    assert after.session is not None and after.session.is_review


def test_a_lesson_session_is_not_a_review() -> None:
    assert not Session(lesson_id=uuid.uuid4()).is_review


# -------------------------------------------------------------- weak words (D-134)


def test_weak_characters_are_bracketed_in_the_sentence() -> None:
    words = (("书", False), ("在", False), ("桌", True), ("子", False), ("上", False))

    assert mark_weak("书在桌子上。", words) == "书在【桌】子上。"


def test_neighbouring_weak_words_share_a_bracket() -> None:
    words = (("书", False), ("在", False), ("桌", True), ("子", True), ("上", False))

    assert mark_weak("书在桌子上。", words) == "书在【桌子】上。"


def test_a_multi_character_word_is_marked_whole() -> None:
    """Azure segments Chinese into words, not characters."""
    words = (("书", False), ("在", False), ("桌子", True), ("上", False))

    assert mark_weak("书在桌子上。", words) == "书在【桌子】上。"


def test_a_repeated_character_is_marked_where_it_was_said() -> None:
    words = (("常", False), ("常", True))

    assert mark_weak("常常", words) == "常【常】"


def test_a_word_not_in_the_sentence_is_skipped_not_guessed() -> None:
    """An insertion, or a provider that tokenised differently from the text."""
    words = (("我", False), ("呢", True), ("要", True), ("水", False))

    assert mark_weak("我要水", words) == "我【要】水"


def _api_words(*words: tuple[str, float, str | None]) -> list[dict[str, object]]:
    """The API's words, as the attempt response carries them. ``weak`` is the
    API's own rule (D-134): misread, omitted, or below 60."""
    return [
        {
            "word": word,
            "accuracy": accuracy,
            "error_type": error_type,
            "weak": error_type in ("Mispronunciation", "Omission") or accuracy < 60,
        }
        for word, accuracy, error_type in words
    ]


@pytest.mark.parametrize(
    ("reference", "words", "expected"),
    [
        (
            # Tones wrong; Azure heard 里 在杯子里. The added 里 used to match
            # the sentence's last character and leave nothing marked.
            "水在杯子里。",
            _api_words(
                ("里", 73, "Insertion"),
                ("水", 0, "Omission"),
                ("在", 100, None),
                ("杯子", 58, "Mispronunciation"),
                ("里", 88, None),
            ),
            "【水】在【杯子】里。",
        ),
        (
            # Word order wrong: 书在上 桌子.
            "书在桌子上。",
            _api_words(
                ("书", 63, None),
                ("在", 97, None),
                ("上", 70, "Insertion"),
                ("桌子", 79, None),
                ("上", 0, "Omission"),
            ),
            "书在桌子【上】。",
        ),
        (
            # Said right: nothing to point at.
            "书在桌子上。",
            _api_words(("书", 68, None), ("在", 100, None), ("桌子", 100, None), ("上", 66, None)),
            None,
        ),
    ],
    ids=["insertion-repeats-a-later-character", "word-order", "said-right"],
)
def test_attempts_from_the_first_trial_are_marked(
    reference: str, words: list[dict[str, object]], expected: str | None
) -> None:
    """Q10 (D-146): the API's words from the owner's first attempts on the
    deployed bot, 2026-11-06, scored by Azure."""
    assert mark_weak(reference, sentence_words(words)) == expected


def test_only_insertions_are_left_out() -> None:
    words = _api_words(("我", 90, None), ("呢", 80, "Insertion"), ("要", 0, "Omission"))

    assert sentence_words(words) == (("我", False), ("要", True))


def test_nothing_weak_means_nothing_to_show() -> None:
    assert mark_weak("我要水", (("我", False), ("要", False), ("水", False))) is None
    assert mark_weak("", (("我", True),)) is None


def test_the_marked_sentence_follows_the_score_before_a_retry() -> None:
    unit = lesson("drill", "vocab")
    session = Session(lesson_id=unit.lesson_id)

    turn = on_voice_scored(
        unit, session, passed=False, score=40.0, max_retry=MAX_RETRY, marked="我【要】水"
    )

    assert keys(turn) == ["bot.score_failed_retry", "bot.weak_words", "bot.prompt_drill"]
    assert turn.replies[1].params == {"text": "我【要】水"}


def test_the_marked_sentence_follows_a_pass_too() -> None:
    """Passing overall can still hide one character worth hearing again."""
    unit = lesson("drill", "vocab")
    session = Session(lesson_id=unit.lesson_id)

    turn = on_voice_scored(
        unit, session, passed=True, score=75.0, max_retry=MAX_RETRY, marked="我【要】水"
    )

    assert keys(turn) == ["bot.score_passed", "bot.weak_words", "bot.prompt_vocab"]


def test_no_marked_sentence_no_extra_line() -> None:
    unit = lesson("drill", "vocab")
    session = Session(lesson_id=unit.lesson_id)

    turn = on_voice_scored(unit, session, passed=False, score=40.0, max_retry=MAX_RETRY)

    assert "bot.weak_words" not in keys(turn)


# ------------------------------------------------------------ masked drills (D-135)

M = MASK


def drilling(*rest: str) -> Lesson:
    drill = Step(
        item_id=uuid.uuid4(),
        item_type="drill",
        text="我要水。\nwǒ yào shuǐ\nខ្ញុំចង់បានទឹក",
        audio_url="https://cdn.example/d.mp3",
        target_text="我要水。",
        hint="ខ្ញុំចង់បានទឹក",
    )
    return Lesson(lesson_id=uuid.uuid4(), title="t", steps=(drill, *(step(t) for t in rest)))


def test_the_first_round_hides_every_other_character_and_the_last_hides_all() -> None:
    assert mask("我要水。", round_number=1, rounds=2) == f"{M}要{M}。"
    assert mask("我要水。", round_number=2, rounds=2) == f"{M}{M}{M}。"


def test_punctuation_and_latin_are_never_hidden() -> None:
    assert mask("你好, OK?", round_number=1, rounds=1) == f"{M}{M}, OK?"


def test_round_zero_or_no_rounds_shows_the_sentence() -> None:
    assert mask("我要水。", round_number=0, rounds=2) == "我要水。"
    assert mask("我要水。", round_number=1, rounds=0) == "我要水。"


def test_a_drill_said_in_full_comes_back_with_gaps() -> None:
    unit = drilling("vocab")
    session = Session(lesson_id=unit.lesson_id)

    turn = on_voice_scored(
        unit, session, passed=True, score=90.0, max_retry=MAX_RETRY, masked_rounds=2
    )

    assert keys(turn) == ["bot.score_passed", "bot.prompt_drill"]
    prompt = turn.replies[-1]
    assert prompt.params == {"text": f"{M}要{M}。\nខ្ញុំចង់បានទឹក"}
    assert prompt.audio_url is None, "the recording would give the gaps away"
    assert turn.session is not None and turn.session.step_index == 0
    assert turn.session.mask_round == 1


def test_the_last_round_hides_everything_and_passing_it_moves_on() -> None:
    unit = drilling("vocab")
    second = on_voice_scored(
        unit,
        Session(lesson_id=unit.lesson_id, mask_round=1),
        passed=True,
        score=90.0,
        max_retry=MAX_RETRY,
        masked_rounds=2,
    )
    assert second.replies[-1].params == {"text": f"{M}{M}{M}。\nខ្ញុំចង់បានទឹក"}
    assert second.session is not None

    done = on_voice_scored(
        unit, second.session, passed=True, score=90.0, max_retry=MAX_RETRY, masked_rounds=2
    )

    assert keys(done) == ["bot.score_passed", "bot.prompt_vocab"]
    assert done.session is not None and done.session.mask_round == 0


def test_a_retry_in_a_masked_round_keeps_the_gaps() -> None:
    unit = drilling("vocab")
    session = Session(lesson_id=unit.lesson_id, mask_round=1)

    turn = on_voice_scored(
        unit, session, passed=False, score=30.0, max_retry=MAX_RETRY, masked_rounds=2
    )

    assert turn.replies[-1].params == {"text": f"{M}要{M}。\nខ្ញុំចង់បានទឹក"}
    assert turn.session is not None and turn.session.mask_round == 1


def test_running_out_of_tries_on_a_masked_round_shows_the_sentence() -> None:
    unit = drilling("vocab")
    session = Session(lesson_id=unit.lesson_id, mask_round=2, attempts_used=MAX_RETRY)

    turn = on_voice_scored(
        unit, session, passed=False, score=30.0, max_retry=MAX_RETRY, masked_rounds=2
    )

    assert keys(turn)[:2] == ["bot.score_failed_moving_on", "bot.model_answer"]
    assert turn.replies[1].params == {"text": "我要水。"}
    assert turn.replies[1].audio_url == "https://cdn.example/d.mp3"


def test_vocab_is_not_masked() -> None:
    unit = lesson("vocab", "drill")
    session = Session(lesson_id=unit.lesson_id)

    turn = on_voice_scored(
        unit, session, passed=True, score=90.0, max_retry=MAX_RETRY, masked_rounds=2
    )

    assert turn.session is not None and turn.session.step_index == 1


def test_no_rounds_configured_moves_straight_on() -> None:
    unit = drilling("vocab")
    session = Session(lesson_id=unit.lesson_id)

    turn = on_voice_scored(unit, session, passed=True, score=90.0, max_retry=MAX_RETRY)

    assert keys(turn) == ["bot.score_passed", "bot.prompt_vocab"]


def test_resuming_mid_round_shows_the_gaps_the_learner_had_reached() -> None:
    unit = drilling("vocab")
    session = Session(lesson_id=unit.lesson_id, mask_round=2)

    turn = resume(unit, session, masked_rounds=2)

    assert turn.replies[-1].params == {"text": f"{M}{M}{M}。\nខ្ញុំចង់បានទឹក"}
    assert turn.session is not None and turn.session.mask_round == 2
