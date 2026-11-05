"""What the learner reads for a step, from the payload keys the importer writes.

The end-to-end test (backend/tests/integration/test_pipeline_to_bot.py) proves
real content renders; these pin down the edges it does not reach.
"""

from __future__ import annotations

from app.core.payloads import explain_payload, qa_payload, spoken_payload
from bot.api_client import text_of


def test_a_drill_shows_the_sentence_its_reading_and_its_meaning() -> None:
    payload = spoken_payload(target_text="我要水。", pinyin="wǒ yào shuǐ", km_gloss="ខ្ញុំចង់បានទឹក។")

    assert text_of("drill", payload).splitlines() == ["我要水。", "wǒ yào shuǐ", "ខ្ញុំចង់បានទឹក។"]


def test_an_explanation_shows_the_pattern_then_the_khmer() -> None:
    payload = explain_payload(pattern="我要 + [名词]", km_explanation="ការពន្យល់")

    assert text_of("explain", payload).splitlines() == ["我要 + [名词]", "ការពន្យល់"]


def test_a_question_shows_the_chinese_and_the_khmer_but_not_the_answer() -> None:
    """The sample answer is what the learner is meant to produce."""
    payload = qa_payload(
        hskk_task_type="listen_and_answer",
        prompt_zh="你要什么?",
        prompt_km="តើអ្នកចង់បានអ្វី?",
        sample_answer_zh="我要水。",
    )

    assert "我要水。" not in text_of("qa", payload)
    assert "你要什么?" in text_of("qa", payload)


def test_empty_fields_are_left_out_rather_than_shown_as_blank_lines() -> None:
    payload = spoken_payload(target_text="我要水。", pinyin="  ", km_gloss="")

    assert text_of("vocab", payload) == "我要水。"


def test_a_question_carries_its_answer_for_after_and_nothing_else_does() -> None:
    from bot.api_client import _step_from

    question = _step_from(
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "item_type": "qa",
            "payload": qa_payload(
                hskk_task_type="listen_and_repeat",
                prompt_zh="请说:我要水。",
                prompt_km="សូមនិយាយ",
                sample_answer_zh="我要水。",
                sample_answer_audio_url="https://cdn.example/a.mp3",
            ),
        }
    )
    drill = _step_from(
        {
            "id": "00000000-0000-0000-0000-000000000002",
            "item_type": "drill",
            "payload": spoken_payload(target_text="我要水。", pinyin="", km_gloss=""),
        }
    )

    assert (question.answer_text, question.answer_audio_url) == (
        "我要水。",
        "https://cdn.example/a.mp3",
    )
    assert (drill.answer_text, drill.answer_audio_url) == ("", None)


def test_an_unknown_kind_of_step_renders_empty_rather_than_failing() -> None:
    assert text_of("roleplay", {"anything": "x"}) == ""
