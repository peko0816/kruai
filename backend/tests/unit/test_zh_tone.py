"""Mandarin tone, derived from syllable accuracy (scoring/zh_tone.py, D-140)."""

from __future__ import annotations

import pytest

from app.services.provider_errors import ProviderConfigurationError
from app.services.scoring.base import PhonemeScore, WordScore
from app.services.scoring.zh_tone import check_tone_mode, tone_score


def word(text: str, *accuracies: float, error_type: str | None = None) -> WordScore:
    return WordScore(
        word=text,
        accuracy=0.0,
        error_type=error_type,
        phonemes=tuple(
            PhonemeScore(phoneme=f"x {i}", accuracy=a) for i, a in enumerate(accuracies)
        ),
    )


def test_the_score_is_the_mean_over_every_syllable_not_every_word() -> None:
    """你好 carries two syllables and 吗 one: three syllables, equal weight."""
    words = [word("你好", 90.0, 60.0), word("吗", 100.0)]

    assert tone_score(words) == pytest.approx(83.3)


def test_an_omitted_syllable_pulls_the_score_down() -> None:
    """A tone never said was not said right."""
    said = [word("我", 100.0), word("要", 100.0)]
    omitted = [*said, word("水", 0.0, error_type="Omission")]

    assert tone_score(said) == 100.0
    assert tone_score(omitted) == pytest.approx(66.7)


@pytest.mark.parametrize(
    "words",
    [[], [WordScore(word="水", accuracy=0.0, error_type="Omission")]],
    ids=["nothing", "no-phonemes"],
)
def test_no_syllables_is_no_score_not_zero(words: list[WordScore]) -> None:
    """None, not 0: absent evidence must not read as a failed tone."""
    assert tone_score(words) is None


@pytest.mark.parametrize("mode", ["derive", "auto"])
def test_derive_and_auto_are_accepted(mode: str) -> None:
    check_tone_mode(mode)  # type: ignore[arg-type]


def test_parse_is_refused_naming_the_decision() -> None:
    with pytest.raises(ProviderConfigurationError, match="D-140"):
        check_tone_mode("parse")
