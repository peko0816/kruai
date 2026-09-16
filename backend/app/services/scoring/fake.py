"""Deterministic stand-in for a real pronunciation scorer.

This is not a stub that returns something plausible — it is the reference
implementation the whole system is developed against until M0-1 picks a vendor
(CLAUDE.md section 4). Gate G-B requires every path to work with providers set
to fake, so this has to behave like a real scorer in every way that callers can
observe: same inputs give the same scores, failures arrive as ok=False rather
than exceptions, and every call reports a cost the ledger can record.

Scores come from a SHA-256 digest of the inputs, not Python's ``hash()``.
ARCHITECTURE 3.2 describes the rule as ``hash(reference_text + len(audio))``,
but ``hash()`` on a str is salted per process (PYTHONHASHSEED), which would
make two runs disagree — exactly the randomness CLAUDE.md section 4 forbids.
tests/unit/test_scoring_fake.py runs a subprocess with a different seed to keep
that from creeping back in.
"""

from __future__ import annotations

import hashlib
from typing import Final

from app.services.scoring.base import (
    AssessMode,
    Language,
    PronunciationResult,
    PronunciationScorer,
    WordScore,
)

#: Putting this in reference_text forces a failing score, so callers can
#: exercise the retry and no-quota-charged branches without special-casing the
#: scorer (ARCHITECTURE 3.2). Exported so tests never hardcode the literal.
BAD_MARKER: Final = "__BAD__"

#: Bands chosen to sit clearly either side of any plausible pass mark. The
#: default SCORING_PASS_THRESHOLD is 60, and M0-1 will recalibrate it against
#: real score distributions; keeping a wide gap means that recalibration cannot
#: silently flip what the fake means by "good" and "bad".
_GOOD_BAND: Final = (80.0, 98.0)
_BAD_BAND: Final = (10.0, 40.0)

#: Synthetic per-call cost. Zero would leave the cost_ledger path (BACKLOG B6)
#: and the PRD 11.2 guardrails untestable under an all-fake configuration,
#: which is the one configuration CI runs. Rounded up rather than down, per the
#: base contract's "conservatively overestimate when unknown". Not a business
#: threshold — those live in CONFIG_REFERENCE.md.
_COST_USD_CENTS: Final = 1


def _stable_digest(reference_text: str | None, audio: bytes) -> int:
    """A process-independent integer derived from the inputs."""
    digest = hashlib.sha256()
    digest.update((reference_text or "").encode("utf-8"))
    digest.update(b"\x00")  # keeps "ab" + 1 from colliding with "a" + 12
    digest.update(str(len(audio)).encode("ascii"))
    return int.from_bytes(digest.digest()[:8], "big")


def _band_value(digest: int, shift: int, band: tuple[float, float]) -> float:
    """Pick a stable value inside ``band``, varying by ``shift``."""
    low, high = band
    steps = round((high - low) * 10) + 1
    return round(low + ((digest >> shift) % steps) / 10, 1)


def _split_words(text: str, language: Language) -> tuple[str, ...]:
    """Khmer and Chinese have no inter-word spaces, so zh splits per character."""
    if language is Language.ZH_CN:
        return tuple(char for char in text if not char.isspace())
    return tuple(text.split())


class FakeScorer(PronunciationScorer):
    """Scores audio by hashing its inputs. No IO, no vendor, no randomness."""

    name = "fake"

    def supports(self, language: Language) -> bool:
        """Every language, so an all-fake configuration can always boot."""
        return isinstance(language, Language)

    async def assess(
        self,
        audio: bytes,
        *,
        language: Language,
        mode: AssessMode,
        reference_text: str | None = None,
        audio_format: str = "wav",
    ) -> PronunciationResult:
        invalid = self._reject(audio, mode=mode, reference_text=reference_text)
        if invalid is not None:
            return invalid

        failing = reference_text is not None and BAD_MARKER in reference_text
        band = _BAD_BAND if failing else _GOOD_BAND
        digest = _stable_digest(reference_text, audio)

        # The marker is a test control, not content: strip it so asr_text and
        # the word list read like a real assessment of the underlying phrase.
        spoken = (reference_text or "").replace(BAD_MARKER, "")

        return PronunciationResult(
            ok=True,
            asr_text=spoken if mode is AssessMode.SCRIPTED else "",
            pron_score=_band_value(digest, 0, band),
            accuracy_score=_band_value(digest, 8, band),
            fluency_score=_band_value(digest, 16, band),
            # Completeness needs a reference to compare against.
            completeness_score=(
                _band_value(digest, 24, band) if mode is AssessMode.SCRIPTED else None
            ),
            # Azure returns prosody for en-US only; zh-CN carries tone instead.
            # The fake mirrors that asymmetry so callers cannot quietly depend
            # on a field the real provider will not send (PRD section 8).
            prosody_score=_band_value(digest, 32, band) if language is Language.EN_US else None,
            tone_score=_band_value(digest, 40, band) if language is Language.ZH_CN else None,
            words=self._words(spoken, digest, language, mode, band),
            raw={
                "provider": self.name,
                "digest": f"{digest:016x}",
                "language": language.value,
                "mode": mode.value,
                "audio_format": audio_format,
                "audio_bytes": len(audio),
            },
            cost_usd_cents=_COST_USD_CENTS,
        )

    def _reject(
        self, audio: bytes, *, mode: AssessMode, reference_text: str | None
    ) -> PronunciationResult | None:
        """Contract violations and empty recordings, as ok=False rather than raises.

        Callers treat ok=False as "provider failed": no quota charged, no
        attempt row written (ARCHITECTURE section 5). Returning it here means
        that branch is reachable in tests without a broken vendor.
        """
        if not audio:
            return _failure("scoring.empty_audio", "audio is empty")
        if mode is AssessMode.SCRIPTED and reference_text is None:
            return _failure("scoring.reference_required", "SCRIPTED mode needs a reference_text")
        if mode is AssessMode.UNSCRIPTED and reference_text is not None:
            return _failure(
                "scoring.reference_not_allowed",
                "UNSCRIPTED mode must not carry a reference_text",
            )
        return None

    def _words(
        self,
        spoken: str,
        digest: int,
        language: Language,
        mode: AssessMode,
        band: tuple[float, float],
    ) -> tuple[WordScore, ...]:
        """Per-word accuracy for scripted calls; nothing to align against otherwise.

        Phonemes stay empty on purpose. Their shape is whatever the chosen
        vendor emits, and M0-1 has not answered that yet (it is the same
        question ZH_TONE_MODE turns on). Inventing one now would bake a guess
        into every test that reads it.
        """
        if mode is not AssessMode.SCRIPTED or not spoken:
            return ()
        return tuple(
            WordScore(
                word=word,
                accuracy=_band_value(digest, index % 7, band),
                error_type=None,
                phonemes=(),
            )
            for index, word in enumerate(_split_words(spoken, language))
        )


def _failure(code: str, message: str) -> PronunciationResult:
    """No cost: nothing was assessed, so nothing should reach the ledger."""
    return PronunciationResult(ok=False, error_code=code, error_message=message, cost_usd_cents=0)


__all__ = ["BAD_MARKER", "FakeScorer"]
