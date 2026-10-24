"""M0-1's verification script (m0/scoring_check.py).

The script's job is to answer whether a threshold can exist between a good
speaker and a real beginner. Its own job is to be trustworthy before anybody
has a key, because the alternative is debugging it against a billed provider
with twenty recordings that took a week to collect.

So the statistics are tested as functions over numbers, not through the
scorer: FakeScorer returns the same value for the same reference text, which
makes it useless for exercising a spread and would quietly turn any test
built on it into a test of nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.services.scoring.base import PhonemeScore, PronunciationResult, WordScore
from m0.scoring_check import (
    BAD,
    GOOD,
    MAX_OVERLAP,
    MIN_MEAN_GAP,
    ManifestError,
    Report,
    Sample,
    Scored,
    as_json,
    as_markdown,
    load_manifest,
    main,
    overlap,
    tone_mode_of,
)


def settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "database_url": "postgresql://unused/unused",
        "redis_url": "redis://unused",
        "telegram_bot_token": "",
        "azure_speech_key": "",
        "azure_speech_region": "",
        "google_application_credentials": "",
        "elevenlabs_api_key": "",
        "openai_api_key": "",
        "payway_merchant_id": "",
        "payway_api_key": "",
        "payway_base_url": "",
        "bakong_token": "",
        "jwt_secret": "x" * 32,
    }
    return Settings(_env_file=None, **{**base, **overrides})


def scored(group: str, score: float | None, *, phonemes: tuple[str, ...] = ()) -> Scored:
    words = (
        (
            WordScore(
                word="x",
                accuracy=score or 0.0,
                phonemes=tuple(PhonemeScore(phoneme=p, accuracy=90.0) for p in phonemes),
            ),
        )
        if phonemes
        else ()
    )
    return Scored(
        sample=Sample(path=Path(f"{group}.wav"), reference_text="我要水。", group=group),
        result=PronunciationResult(
            ok=score is not None,
            pron_score=score,
            words=words,
            error_code=None if score is not None else "scoring.unavailable",
            cost_usd_cents=1,
        ),
    )


def report(good: list[float], bad: list[float], **kwargs: Any) -> Report:
    return Report(
        tuple([scored(GOOD, s, **kwargs) for s in good] + [scored(BAD, s, **kwargs) for s in bad])
    )


# ------------------------------------------------------------------ the manifest


def manifest(tmp_path: Path, body: str, *, files: tuple[str, ...] = ()) -> Path:
    for name in files:
        (tmp_path / name).write_bytes(b"RIFF")
    path = tmp_path / "manifest.csv"
    path.write_text(body, encoding="utf-8")
    return path


def test_a_well_formed_manifest_loads(tmp_path: Path) -> None:
    path = manifest(
        tmp_path,
        "file,reference_text,group\na.wav,我要水。,good\nb.wav,我要水。,bad\n",
        files=("a.wav", "b.wav"),
    )

    samples = load_manifest(path)

    assert [s.group for s in samples] == [GOOD, BAD]
    assert samples[0].path.name == "a.wav"
    assert samples[0].audio_format == "wav"


def test_every_problem_is_reported_at_once(tmp_path: Path) -> None:
    """Somebody assembling twenty recordings by hand will have several wrong,
    and finding them one run at a time is the slowest way to learn that."""
    path = manifest(
        tmp_path,
        "file,reference_text,group\n"
        "missing.wav,我要水。,good\n"
        "b.wav,,good\n"
        "b.wav,我要水。,neither\n",
        files=("b.wav",),
    )

    with pytest.raises(ManifestError) as error:
        load_manifest(path)

    problems = error.value.problems
    assert len(problems) == 4  # missing file, no text, bad group, and no bad group at all
    assert any("does not exist" in p for p in problems)
    assert any("reference_text" in p for p in problems)
    assert any("neither" in p for p in problems)


def test_a_manifest_with_one_group_is_refused(tmp_path: Path) -> None:
    """The whole measurement is a comparison; one group measures nothing."""
    path = manifest(tmp_path, "file,reference_text,group\na.wav,我要水。,good\n", files=("a.wav",))

    with pytest.raises(ManifestError, match="needs both"):
        load_manifest(path)


def test_a_file_that_is_not_the_manifest_is_refused(tmp_path: Path) -> None:
    path = manifest(tmp_path, "a,b\n1,2\n")

    with pytest.raises(ManifestError, match="no column"):
        load_manifest(path)


def test_paths_are_relative_to_the_manifest(tmp_path: Path) -> None:
    """So a folder of recordings and its manifest can be moved together."""
    (tmp_path / "clips").mkdir()
    (tmp_path / "clips" / "a.wav").write_bytes(b"RIFF")
    (tmp_path / "clips" / "b.wav").write_bytes(b"RIFF")
    path = tmp_path / "clips" / "manifest.csv"
    path.write_text(
        "file,reference_text,group\na.wav,我要水。,good\nb.wav,我要水。,bad\n", encoding="utf-8"
    )

    assert load_manifest(path)[0].path == (tmp_path / "clips" / "a.wav").resolve()


# -------------------------------------------------------------------- the overlap


def test_two_separated_groups_do_not_overlap() -> None:
    assert overlap([80, 85, 90], [10, 20, 30]) == 0.0


def test_one_good_speaker_below_the_best_beginner_counts() -> None:
    """The blunt question this answers: can a line be drawn? A good speaker
    under the best beginner is on the wrong side of every line there is."""
    assert overlap([80, 85, 35], [10, 20, 40]) > 0


def test_identical_groups_overlap_entirely() -> None:
    assert overlap([50, 50], [50, 50]) == 1.0


def test_an_empty_group_is_total_overlap_not_a_crash() -> None:
    """Nothing to compare is not evidence of separation."""
    assert overlap([], [10, 20]) == 1.0


# ------------------------------------------------------------------- the tone mode


def test_phonemes_with_tone_digits_mean_parse() -> None:
    assert tone_mode_of([scored(GOOD, 90.0, phonemes=("ni3", "hao3"))]) == "parse"


def test_phonemes_without_tone_digits_mean_derive() -> None:
    assert tone_mode_of([scored(GOOD, 90.0, phonemes=("ni", "hao"))]) == "derive"


def test_no_phonemes_at_all_is_its_own_answer() -> None:
    """E9 can neither parse nor derive without them, so it is a finding rather
    than a default."""
    assert tone_mode_of([scored(GOOD, 90.0)]) == "unknown"


# --------------------------------------------------------------- the verdicts


def test_a_clean_separation_passes() -> None:
    assert report([88, 90, 92], [20, 25, 30], phonemes=("ni3",)).passed


def test_a_gap_below_the_bar_fails() -> None:
    r = report([60, 62, 64], [55, 56, 57], phonemes=("ni3",))

    assert r.mean_gap < MIN_MEAN_GAP
    assert not r.passed


def test_a_wide_gap_with_heavy_overlap_still_fails() -> None:
    """The case a difference of means hides: the averages are far apart and
    the samples are interleaved, so no threshold separates them."""
    r = report([95, 95, 95, 20], [90, 10, 10, 10], phonemes=("ni3",))

    assert r.mean_gap >= MIN_MEAN_GAP
    assert r.overlap > MAX_OVERLAP
    assert not r.passed


def test_missing_phoneme_detail_fails_even_with_a_clean_gap() -> None:
    """E9 needs it whichever way tone turns out, so a run without it has not
    answered the question it was run for."""
    r = report([88, 90, 92], [20, 25, 30])

    assert r.mean_gap >= MIN_MEAN_GAP
    assert not r.passed


def test_a_sample_that_did_not_score_is_counted_not_dropped() -> None:
    """Silently averaging what came back would report a clean result on a run
    where half the calls failed.

    Every other criterion is satisfied here on purpose — phonemes included —
    so the only thing that can fail is the unscored sample. Without that, the
    missing-phoneme criterion carried the assertion and a mutation removing
    this check survived.
    """
    r = Report(
        (
            scored(GOOD, 90.0, phonemes=("ni3",)),
            scored(GOOD, None, phonemes=("ni3",)),
            scored(BAD, 20.0, phonemes=("ni3",)),
        )
    )

    assert r.mean_gap >= MIN_MEAN_GAP
    assert r.overlap <= MAX_OVERLAP
    assert r.has_phonemes
    assert len(r.failed) == 1
    assert not r.passed


# ----------------------------------------------------------------- the report


def test_the_report_states_what_each_number_was_judged_against() -> None:
    """It is read later by whoever sets the threshold, and a bare number is
    not evidence."""
    text = as_markdown(report([88, 90], [20, 25], phonemes=("ni3",)), provider="azure")

    assert "mean gap >= 15" in text
    assert "overlap <= 20%" in text
    assert "`azure`" in text


def test_the_report_says_which_tone_mode_to_set() -> None:
    text = as_markdown(report([88], [20], phonemes=("ni3",)), provider="azure")

    assert "`parse`" in text


def test_the_report_says_what_it_has_not_settled() -> None:
    """A gap says a threshold can exist, not where it goes — and L-7 says the
    threshold cannot move alone."""
    text = as_markdown(report([88], [20], phonemes=("ni3",)), provider="azure")

    assert "L-7" in text
    assert "MASTERY_DELTA_BASE" in text


def test_a_failing_sample_is_named_in_the_table() -> None:
    text = as_markdown(Report((scored(GOOD, None), scored(BAD, 20.0))), provider="azure")

    assert "scoring.unavailable" in text


def test_the_json_carries_the_same_verdict() -> None:
    r = report([88, 90], [20, 25], phonemes=("ni3",))

    assert as_json(r, provider="azure")["passed"] is r.passed
    assert as_json(r, provider="azure")["tone_mode"] == "parse"


# ------------------------------------------------------------------- the CLI


def test_a_billed_provider_needs_the_spend_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Section 6: the difference between a free run and a billed one is one
    line in .env that nobody re-reads before typing the command."""
    import m0.scoring_check as module

    monkeypatch.setattr(module, "get_settings", lambda: settings(scoring_provider="azure"))
    path = manifest(
        tmp_path,
        "file,reference_text,group\na.wav,我要水。,good\nb.wav,我要水。,bad\n",
        files=("a.wav", "b.wav"),
    )

    assert main([str(path)]) == 2
    assert "bills per call" in capsys.readouterr().err


def test_the_fake_provider_runs_without_the_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of the abstraction: every line of this runs before anyone has
    a key, which is why it can be trusted when the recordings finally arrive."""
    import m0.scoring_check as module

    monkeypatch.setattr(module, "get_settings", lambda: settings())
    out = tmp_path / "report.md"
    path = manifest(
        tmp_path,
        "file,reference_text,group\na.wav,我要水。,good\nb.wav,我要水。__BAD__,bad\n",
        files=("a.wav", "b.wav"),
    )

    main([str(path), "--out", str(out)])

    assert "M0-1" in out.read_text(encoding="utf-8")


def test_the_exit_code_says_whether_m0_1_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """So it can gate something later without anybody parsing the Markdown."""
    import m0.scoring_check as module

    monkeypatch.setattr(module, "get_settings", lambda: settings())
    path = manifest(
        tmp_path,
        "file,reference_text,group\na.wav,我要水。,good\nb.wav,我要水。__BAD__,bad\n",
        files=("a.wav", "b.wav"),
    )

    # FakeScorer separates the bands cleanly but returns no phonemes, so this
    # is a real failure of a real criterion rather than a broken run.
    assert main([str(path), "--out", str(tmp_path / "r.md")]) == 1
