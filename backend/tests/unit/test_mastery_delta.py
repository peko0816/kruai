"""PRD 9.2's formula, including the parts that only bite at the edges.

Mastery is the one number the whole product is built to move, and it is stored
rather than recomputed — so an error here does not show up as a wrong answer,
it accumulates silently in the database and quietly reshapes what every learner
is shown next. That is why the boundaries get as much attention as the happy
path, and why several tests change the configuration and assert the output
followed: "all parameters come from configuration" is only true if changing one
changes the result.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.config import Settings
from app.services.mastery import (
    MASTERY_CEILING,
    MASTERY_FLOOR,
    apply_attempt,
    clamp_mastery,
    is_passing,
    score_delta,
    weight_for,
)

_MINIMAL: dict[str, str] = {
    "DATABASE_URL": "postgresql+psycopg://u:p@localhost:5432/db",
    "REDIS_URL": "redis://localhost:6379/0",
    "TELEGRAM_BOT_TOKEN": "",
    "AZURE_SPEECH_KEY": "",
    "AZURE_SPEECH_REGION": "",
    "GOOGLE_APPLICATION_CREDENTIALS": "",
    "ELEVENLABS_API_KEY": "",
    "OPENAI_API_KEY": "",
    "PAYWAY_MERCHANT_ID": "",
    "PAYWAY_API_KEY": "",
    "PAYWAY_BASE_URL": "",
    "BAKONG_TOKEN": "",
    "JWT_SECRET": "",
}


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


def settings(**overrides: str) -> Settings:
    values: dict[str, Any] = {**_MINIMAL, **overrides}
    return Settings(_env_file=None, **values)


# ------------------------------------------------------------ the delta curve


@pytest.mark.parametrize(
    ("pron_score", "expected"),
    [
        (100.0, 1.0),  # 满分
        (80.0, 0.5),
        (60.0, 0.0),  # exactly the pass mark: no evidence either way
        (40.0, -0.5),
        (0.0, -1.5),  # 零分
    ],
)
def test_delta_matches_the_documented_curve(pron_score: float, expected: float) -> None:
    """PRD 9.2 says this maps to [-1.5, +1.0]; these are the ends and the middle."""
    assert score_delta(pron_score, settings=settings()) == pytest.approx(expected)


def test_the_scale_is_asymmetric() -> None:
    """A silent attempt costs more than a perfect one earns, because failing to
    produce a sentence is stronger evidence than producing it once."""
    config = settings()
    assert abs(score_delta(0.0, settings=config)) > score_delta(100.0, settings=config)


def test_a_failing_score_gives_a_negative_delta() -> None:
    assert score_delta(30.0, settings=settings()) < 0


@pytest.mark.parametrize("pron_score", [-0.1, 100.1, -50.0, 1000.0])
def test_a_score_outside_the_provider_contract_is_refused(pron_score: float) -> None:
    """One malformed response would otherwise move mastery twenty points in a
    single step, and nothing downstream would look wrong."""
    with pytest.raises(ValueError, match="pron_score must be within"):
        score_delta(pron_score, settings=settings())


# ----------------------------------------------------------------- the weights


@pytest.mark.parametrize(
    ("item_type", "expected"),
    [("drill", 0.6), ("vocab", 0.8), ("qa", 1.0)],
)
def test_weights_match_prd_9_2(item_type: str, expected: float) -> None:
    assert weight_for(item_type, settings=settings()) == pytest.approx(expected)


def test_weight_rises_with_closeness_to_real_use() -> None:
    """The ordering is the point, not the numbers: a free answer counts for more
    than reading a sentence back."""
    config = settings()
    drill = weight_for("drill", settings=config)
    vocab = weight_for("vocab", settings=config)
    qa = weight_for("qa", settings=config)

    assert drill < vocab < qa


def test_an_explain_card_has_no_weight() -> None:
    """There is nothing to say back to a lecture card, so it produces no attempt."""
    with pytest.raises(ValueError, match="'explain' has no mastery weight"):
        weight_for("explain", settings=settings())


def test_an_unknown_item_type_names_the_valid_ones() -> None:
    with pytest.raises(ValueError, match=r"\['drill', 'qa', 'vocab'\]"):
        weight_for("typo", settings=settings())


# ---------------------------------------------------------------- the clamp


@pytest.mark.parametrize(
    ("value", "expected"),
    [(-10.0, 0.0), (0.0, 0.0), (50.0, 50.0), (100.0, 100.0), (150.0, 100.0)],
)
def test_clamp_holds_the_scale(value: float, expected: float) -> None:
    assert clamp_mastery(value) == expected


def test_a_perfect_attempt_at_the_ceiling_changes_nothing() -> None:
    """上边界."""
    update = apply_attempt(
        current_mastery=100.0, pron_score=100.0, item_type="qa", settings=settings()
    )
    assert update.current == 100.0
    assert update.clamped is True


def test_a_silent_attempt_at_the_floor_changes_nothing() -> None:
    """下边界."""
    update = apply_attempt(current_mastery=0.0, pron_score=0.0, item_type="qa", settings=settings())
    assert update.current == 0.0
    assert update.clamped is True


def test_the_clamp_flag_distinguishes_pinned_from_stalled() -> None:
    """A learner at the ceiling looks identical to one making no progress unless
    this is recorded."""
    config = settings()
    pinned = apply_attempt(current_mastery=100.0, pron_score=100.0, item_type="qa", settings=config)
    moving = apply_attempt(current_mastery=50.0, pron_score=100.0, item_type="qa", settings=config)

    assert pinned.clamped is True
    assert moving.clamped is False


def test_near_the_ceiling_the_step_is_truncated_not_rejected() -> None:
    update = apply_attempt(
        current_mastery=99.7, pron_score=100.0, item_type="qa", settings=settings()
    )
    assert update.current == 100.0
    assert update.clamped is True


# ----------------------------------------------------------- applying one attempt


def test_a_good_attempt_raises_mastery() -> None:
    update = apply_attempt(
        current_mastery=50.0, pron_score=100.0, item_type="qa", settings=settings()
    )
    assert update.current == pytest.approx(51.0)
    assert update.improved is True


def test_a_bad_attempt_lowers_mastery() -> None:
    """负 delta."""
    update = apply_attempt(
        current_mastery=50.0, pron_score=0.0, item_type="qa", settings=settings()
    )
    assert update.current == pytest.approx(48.5)
    assert update.improved is False


def test_an_attempt_at_the_pass_mark_moves_nothing() -> None:
    update = apply_attempt(
        current_mastery=50.0, pron_score=60.0, item_type="qa", settings=settings()
    )
    assert update.current == pytest.approx(50.0)
    assert update.clamped is False


@pytest.mark.parametrize(
    ("item_type", "expected"),
    [("drill", 50.6), ("vocab", 50.8), ("qa", 51.0)],
)
def test_the_same_score_moves_less_on_a_lighter_item(item_type: str, expected: float) -> None:
    """权重切换: identical score, different item, different progress."""
    update = apply_attempt(
        current_mastery=50.0, pron_score=100.0, item_type=item_type, settings=settings()
    )
    assert update.current == pytest.approx(expected)


def test_the_update_carries_the_terms_that_produced_it() -> None:
    """A mastery score that moved unexpectedly is undiagnosable once the inputs
    are gone and the formula has four terms."""
    update = apply_attempt(
        current_mastery=50.0, pron_score=80.0, item_type="drill", settings=settings()
    )

    assert update.previous == 50.0
    assert update.delta == pytest.approx(0.5)
    assert update.weight == pytest.approx(0.6)
    assert update.weighted_delta == pytest.approx(0.3)
    assert update.current == pytest.approx(50.3)


def test_a_corrupted_stored_mastery_is_refused_rather_than_clamped() -> None:
    """Something already went wrong; a fresh clamp would bury the evidence."""
    with pytest.raises(ValueError, match="current_mastery must be within"):
        apply_attempt(current_mastery=140.0, pron_score=80.0, item_type="qa", settings=settings())


def test_a_new_concept_starts_from_zero() -> None:
    """PRD 9.2: 初始 mastery = 0."""
    update = apply_attempt(
        current_mastery=MASTERY_FLOOR, pron_score=100.0, item_type="qa", settings=settings()
    )
    assert update.current == pytest.approx(1.0)


# ------------------------------------------------------ everything is configured


def test_the_pass_mark_comes_from_configuration() -> None:
    """M0-1 will recalibrate it; a literal 60 in the formula would not follow."""
    strict = settings(SCORING_PASS_THRESHOLD="80")
    assert score_delta(80.0, settings=strict) == pytest.approx(0.0)
    assert score_delta(60.0, settings=strict) < 0


def test_the_delta_base_comes_from_configuration() -> None:
    """Halving it doubles how fast mastery moves."""
    default = score_delta(100.0, settings=settings())
    faster = score_delta(100.0, settings=settings(MASTERY_DELTA_BASE="20"))
    assert faster == pytest.approx(default * 2)


def test_the_weights_come_from_configuration() -> None:
    flat = settings(MASTERY_WEIGHT_DRILL="1.0", MASTERY_WEIGHT_VOCAB="1.0", MASTERY_WEIGHT_QA="1.0")
    moves = {
        apply_attempt(current_mastery=50.0, pron_score=100.0, item_type=item, settings=flat).current
        for item in ("drill", "vocab", "qa")
    }
    assert len(moves) == 1, "with equal weights the item type should stop mattering"


def test_a_zero_weight_freezes_that_item_type() -> None:
    """Config allows ge=0, so this is reachable — a drill could be made to count
    for nothing without touching code."""
    update = apply_attempt(
        current_mastery=50.0,
        pron_score=100.0,
        item_type="drill",
        settings=settings(MASTERY_WEIGHT_DRILL="0"),
    )
    assert update.current == pytest.approx(50.0)


# ---------------------------------------------------------------- passed flag


@pytest.mark.parametrize(
    ("pron_score", "expected"),
    [(100.0, True), (60.0, True), (59.9, False), (0.0, False)],
)
def test_passing_is_decided_at_the_threshold(pron_score: float, expected: bool) -> None:
    assert is_passing(pron_score, settings=settings()) is expected


def test_passing_and_the_delta_agree_on_the_threshold() -> None:
    """An attempt marked passed whose mastery went down is a contradiction
    nobody would think to look for, so both read the same setting."""
    config = settings(SCORING_PASS_THRESHOLD="75")

    for score in (0.0, 50.0, 74.9, 75.0, 90.0, 100.0):
        passed = is_passing(score, settings=config)
        delta = score_delta(score, settings=config)
        assert passed == (delta >= 0), f"disagreement at {score}"


# ------------------------------------------------------------------ over time


def test_a_hundred_perfect_attempts_reach_the_ceiling() -> None:
    """The documented pace, asserted rather than assumed: with weight 1.0 and a
    perfect score the step is one point, so the ceiling is a hundred attempts
    away. If this number is wrong for the product, MASTERY_DELTA_BASE is the
    knob — not the code."""
    config = settings()
    mastery = MASTERY_FLOOR
    for _ in range(100):
        mastery = apply_attempt(
            current_mastery=mastery, pron_score=100.0, item_type="qa", settings=config
        ).current

    assert mastery == pytest.approx(MASTERY_CEILING)


def test_repeated_failures_bottom_out_rather_than_going_negative() -> None:
    config = settings()
    mastery = 5.0
    for _ in range(20):
        mastery = apply_attempt(
            current_mastery=mastery, pron_score=0.0, item_type="qa", settings=config
        ).current

    assert mastery == MASTERY_FLOOR


def test_mixed_attempts_accumulate_in_both_directions() -> None:
    """Two perfect drills then one silent qa: +0.6, +0.6, -1.5."""
    config = settings()
    mastery = 50.0
    for score, item in ((100.0, "drill"), (100.0, "drill"), (0.0, "qa")):
        mastery = apply_attempt(
            current_mastery=mastery, pron_score=score, item_type=item, settings=config
        ).current

    assert mastery == pytest.approx(49.7)
