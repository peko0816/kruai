"""FakeScorer is the provider the whole system is built against until M0-1.

So its contract matters as much as a real one's: determinism, no exceptions,
and a cost on every assessment. The determinism test runs a subprocess with a
different PYTHONHASHSEED — a fake that quietly depended on Python's salted
``hash()`` would pass every in-process check and then disagree with itself
between CI runs.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from app.services.scoring.base import AssessMode, Language, PronunciationResult
from app.services.scoring.fake import BAD_MARKER, FakeScorer

PASS_MARK = 60.0  # the default SCORING_PASS_THRESHOLD callers compare against
AUDIO = b"\x00\x01\x02" * 400


async def score(
    reference_text: str | None = "我要水",
    *,
    audio: bytes = AUDIO,
    language: Language = Language.ZH_CN,
    mode: AssessMode = AssessMode.SCRIPTED,
) -> PronunciationResult:
    return await FakeScorer().assess(
        audio, language=language, mode=mode, reference_text=reference_text
    )


def every_score(result: PronunciationResult) -> list[float]:
    values = [
        result.pron_score,
        result.accuracy_score,
        result.fluency_score,
        result.completeness_score,
        result.prosody_score,
        result.tone_score,
    ]
    return [v for v in values if v is not None]


# ----------------------------------------------------------------- determinism


async def test_same_input_gives_same_result() -> None:
    first, second = await score(), await score()
    assert first == second


async def test_repeated_calls_never_drift() -> None:
    results = [await score() for _ in range(20)]
    assert len({r.pron_score for r in results}) == 1


async def test_different_reference_text_gives_different_score() -> None:
    """A constant would satisfy determinism while testing nothing."""
    scores = {(await score(text)).pron_score for text in ("我要水", "我要茶", "他很高")}
    assert len(scores) > 1


async def test_audio_length_changes_the_score() -> None:
    short, long = await score(audio=b"x" * 100), await score(audio=b"x" * 5000)
    assert short.pron_score != long.pron_score


def test_determinism_survives_a_different_hash_seed() -> None:
    """Python salts str.__hash__ per process; SHA-256 is what keeps this stable."""
    program = (
        "import asyncio;"
        "from app.services.scoring.fake import FakeScorer;"
        "from app.services.scoring.base import Language, AssessMode;"
        "r = asyncio.run(FakeScorer().assess("
        "b'abc'*100, language=Language.ZH_CN, mode=AssessMode.SCRIPTED,"
        " reference_text='我要水'));"
        "print(r.pron_score, r.accuracy_score, r.tone_score)"
    )
    outputs = set()
    for seed in ("0", "1", "12345"):
        completed = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            check=True,
            env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
        )
        outputs.add(completed.stdout.strip())

    assert len(outputs) == 1, f"scores changed with PYTHONHASHSEED: {outputs}"


# -------------------------------------------------------------- the BAD marker


async def test_bad_marker_fails_the_attempt() -> None:
    result = await score(f"我要水{BAD_MARKER}")
    assert result.ok is True
    assert result.pron_score is not None
    assert result.pron_score < PASS_MARK


async def test_bad_marker_drags_down_every_dimension() -> None:
    """One high sub-score would let a caller's averaging accidentally pass."""
    result = await score(f"我要水{BAD_MARKER}", language=Language.EN_US)
    assert all(value < PASS_MARK for value in every_score(result))


async def test_clean_reference_passes_comfortably() -> None:
    result = await score()
    assert all(value > PASS_MARK for value in every_score(result))


async def test_bad_marker_is_stripped_from_the_transcript() -> None:
    """The marker is a test control, not something the learner said."""
    result = await score(f"我要水{BAD_MARKER}")
    assert BAD_MARKER not in result.asr_text
    assert result.asr_text == "我要水"


async def test_bad_marker_is_deterministic_too() -> None:
    assert await score(f"x{BAD_MARKER}") == await score(f"x{BAD_MARKER}")


# ------------------------------------------------- language and mode asymmetry


async def test_chinese_has_tone_but_no_prosody() -> None:
    """Azure returns no ProsodyScore for zh-CN; the fake must not invent one."""
    result = await score(language=Language.ZH_CN)
    assert result.tone_score is not None
    assert result.prosody_score is None


async def test_english_has_prosody_but_no_tone() -> None:
    result = await score("I want water", language=Language.EN_US)
    assert result.prosody_score is not None
    assert result.tone_score is None


async def test_completeness_only_exists_with_a_reference() -> None:
    scripted = await score()
    unscripted = await score(None, mode=AssessMode.UNSCRIPTED)
    assert scripted.completeness_score is not None
    assert unscripted.completeness_score is None


async def test_chinese_words_split_per_character() -> None:
    result = await score("我要水")
    assert [w.word for w in result.words] == ["我", "要", "水"]


async def test_english_words_split_on_whitespace() -> None:
    result = await score("I want water", language=Language.EN_US)
    assert [w.word for w in result.words] == ["I", "want", "water"]


async def test_unscripted_has_no_word_alignment() -> None:
    result = await score(None, mode=AssessMode.UNSCRIPTED)
    assert result.words == ()


async def test_phonemes_are_left_empty_until_m0_defines_them() -> None:
    """Guessing the phoneme shape now would bake it into every downstream test."""
    result = await score()
    assert all(w.phonemes == () for w in result.words)


# ------------------------------------------------------- failures, not raises


@pytest.mark.parametrize(
    ("reference_text", "mode", "expected_code"),
    [
        ("我要水", AssessMode.SCRIPTED, "scoring.empty_audio"),
        (None, AssessMode.UNSCRIPTED, "scoring.empty_audio"),
    ],
)
async def test_empty_audio_is_reported_not_raised(
    reference_text: str | None, mode: AssessMode, expected_code: str
) -> None:
    result = await score(reference_text, audio=b"", mode=mode)
    assert result.ok is False
    assert result.error_code == expected_code


async def test_scripted_without_reference_is_rejected() -> None:
    result = await score(None, mode=AssessMode.SCRIPTED)
    assert result.ok is False
    assert result.error_code == "scoring.reference_required"


async def test_unscripted_with_reference_is_rejected() -> None:
    result = await score("我要水", mode=AssessMode.UNSCRIPTED)
    assert result.ok is False
    assert result.error_code == "scoring.reference_not_allowed"


async def test_failures_carry_no_cost() -> None:
    """Nothing was assessed, so nothing should reach the cost ledger."""
    result = await score(audio=b"")
    assert result.cost_usd_cents == 0


# -------------------------------------------------------------------- contract


async def test_every_assessment_reports_a_cost() -> None:
    """A zero here would leave the ledger and its guardrails untestable."""
    result = await score()
    assert result.cost_usd_cents > 0


async def test_raw_is_populated_but_holds_no_audio() -> None:
    """raw lands in attempts.phoneme_detail; audio content must never be logged."""
    result = await score()
    assert result.raw["provider"] == "fake"
    assert result.raw["audio_bytes"] == len(AUDIO)
    assert not any(isinstance(v, bytes) for v in result.raw.values())


def test_supports_every_language() -> None:
    """An all-fake deployment must boot regardless of configured language."""
    scorer = FakeScorer()
    assert all(scorer.supports(language) for language in Language)


def test_name_is_the_ledger_identifier() -> None:
    assert FakeScorer().name == "fake"


@pytest.mark.parametrize("language", list(Language))
@pytest.mark.parametrize("mode", list(AssessMode))
async def test_scores_stay_within_zero_to_one_hundred(language: Language, mode: AssessMode) -> None:
    reference = "我要水" if mode is AssessMode.SCRIPTED else None
    result = await score(reference, language=language, mode=mode)
    assert all(0.0 <= value <= 100.0 for value in every_score(result))
    assert all(0.0 <= w.accuracy <= 100.0 for w in result.words)
