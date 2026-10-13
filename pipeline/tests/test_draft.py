"""The draft's shape (pipeline/draft.py).

The shape rules were mostly uninteresting — non-empty strings — until a real
run produced a Khmer explanation holding NUL bytes and unit separators. Nothing
caught it: it passed all eight of validate.py's rules, was paid for in TTS, and
failed at the INSERT, which is the one place where the fix costs most and the
error says least.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from pydantic import ValidationError

from pipeline.draft import ConceptDraft, Draft

KM = "ខ្ញុំចង់បានទឹក"


def a_concept(**overrides: Any) -> dict[str, Any]:
    return {
        "slug": "zh.hsk1.want_noun",
        "pattern": "我要 + [名词]",
        "hskk_task_types": ["listen_and_repeat"],
        "km_explanation": KM,
        "target_sentences": [{"zh": "我要水。", "pinyin": "wǒ yào shuǐ", "km_gloss": KM}],
        "substitutions": [{"zh": "水", "pinyin": "shuǐ", "km_gloss": KM}],
        "dialogues": [
            {
                "hskk_task_type": "listen_and_repeat",
                "prompt_zh": "你要水",
                "prompt_km": KM,
                "sample_answer_zh": "我要水",
            }
        ],
        **overrides,
    }


def test_an_ordinary_concept_is_accepted() -> None:
    assert ConceptDraft.model_validate(a_concept()).slug == "zh.hsk1.want_noun"


@pytest.mark.parametrize("control", ["\x00", "\x1f", "\x01", "\x7f", "\x08"])
def test_a_garbled_field_is_refused(control: str) -> None:
    """NUL is the one PostgreSQL refuses outright; the rest reach a learner's
    screen as nothing at all, which is worse because it looks like content."""
    with pytest.raises(ValidationError, match="control character"):
        ConceptDraft.model_validate(a_concept(km_explanation=f"{KM}{control}{KM}"))


@pytest.mark.parametrize("whitespace", ["\t", "\n", "\r"])
def test_ordinary_whitespace_is_not_a_control_character(whitespace: str) -> None:
    """An explanation spanning two lines is an explanation, not a garbled field."""
    assert ConceptDraft.model_validate(a_concept(km_explanation=f"{KM}{whitespace}{KM}"))


@pytest.mark.parametrize(
    "field",
    ["target_sentences", "substitutions", "dialogues"],
)
def test_every_text_field_is_covered_not_just_the_explanation(field: str) -> None:
    """The real run had them in km_gloss as well — eight of eleven, in fact."""
    concept = a_concept()
    item = dict(concept[field][0])
    key = "km_gloss" if field != "dialogues" else "prompt_km"
    item[key] = f"{KM}\x1f"
    with pytest.raises(ValidationError, match="control character"):
        ConceptDraft.model_validate(a_concept(**{field: [item]}))


def test_the_error_says_where_it_is() -> None:
    """A field of Khmer with one invisible byte in it is not something anyone
    finds by reading; the message has to carry the position."""
    with pytest.raises(ValidationError) as error:
        ConceptDraft.model_validate(a_concept(km_explanation=f"{KM}\x00"))

    assert "0x0" in str(error.value)
    assert f"position {len(KM)}" in str(error.value)


def test_a_whole_draft_refuses_one_garbled_concept() -> None:
    payload: dict[str, Any] = {
        "language": "zh",
        "level": "HSK1",
        "licence": "public standards",
        "sources": [{"id": "zh.standard.2021"}],
        "seed_file": "test.yaml",
        "provider": "fake",
        "model": "fake",
        "generated_at": dt.datetime(2026, 9, 19, tzinfo=dt.UTC),
        "usage": {},
        "concepts": [a_concept(), a_concept(km_explanation=f"{KM}\x00")],
    }

    with pytest.raises(ValidationError, match="control character"):
        Draft.model_validate(payload)
