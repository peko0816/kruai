"""Tone in Mandarin, from what a scorer says about each syllable (PRD section 8).

Azure has no tone dimension for zh-CN; PRD section 8 put the tone score here,
with M0-1 to decide whether it could be *parsed* from tone digits in the
phoneme strings or had to be *derived* from phoneme-level accuracy.

M0-1's first real run settled it (D-140): Azure's zh-CN phonemes are pinyin
syllables with a digit — ``qing 3``, ``zuo 4`` — but the digit is the tone
Azure's lexicon **expected** for the reference text, not the one it heard. It
ignores third-tone sandhi (我很 came back ``wo 3``), gives neutral tones their
dictionary tone (还是 → ``shi 4``), and sometimes picks the wrong reading of a
character outright (几 in 你几岁 → ``ji 1``). Parsing that digit would report
the dictionary, not the learner. So the tone score is derived: a syllable's
accuracy is how close the learner came to the syllable as expected, tone
included, and the tone score is the mean over the syllables.

``ZH_TONE_MODE=parse`` is therefore refused, at construction rather than on
the first attempt; ``auto`` means ``derive``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from app.services.provider_errors import ProviderConfigurationError
from app.services.scoring.base import WordScore

ToneMode = Literal["parse", "derive", "auto"]


def check_tone_mode(mode: ToneMode) -> None:
    """Raises ProviderConfigurationError for a mode that cannot work (D-140)."""
    if mode == "parse":
        raise ProviderConfigurationError(
            "ZH_TONE_MODE=parse reads the tone digit in each phoneme, but that digit is "
            "the tone the scorer expected, not the one it heard (D-140); use derive"
        )


def tone_score(words: Sequence[WordScore]) -> float | None:
    """The mean accuracy of the syllables spoken, 0-100; None with no syllables.

    An omitted word's syllables count at whatever the scorer gave them (zero,
    usually): a tone never said was not said right.
    """
    accuracies = [phoneme.accuracy for word in words for phoneme in word.phonemes]
    if not accuracies:
        return None
    return round(sum(accuracies) / len(accuracies), 1)


__all__ = ["ToneMode", "check_tone_mode", "tone_score"]
