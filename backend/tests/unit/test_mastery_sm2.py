"""PRD 9.2's spacing rules, including the two ways they can quietly fail.

The first is starvation: at the minimum ease, rounding an interval down leaves
it at one day forever, so a learner reviews the same concept daily while
passing it every time. Nothing errors — the schedule just stops working.

The second is the band between the thresholds, which PRD 9.2 does not mention.
Getting it wrong is invisible in a unit of one review and only shows up as a
review queue that behaves oddly for mid-range concepts.
"""

from __future__ import annotations

import datetime
from typing import Any

import pytest

from app.core.config import Settings
from app.services.mastery import (
    INITIAL_INTERVAL_DAYS,
    ReviewSchedule,
    initial_ease_factor,
    schedule_review,
)

NOW = datetime.datetime(2026, 9, 16, 12, 0, tzinfo=datetime.UTC)

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


def review(
    mastery: float,
    *,
    ease: float = 2.5,
    interval: int = 1,
    now: datetime.datetime = NOW,
    config: Settings | None = None,
) -> ReviewSchedule:
    return schedule_review(
        mastery=mastery,
        ease_factor=ease,
        interval_days=interval,
        now=now,
        settings=config or settings(),
    )


# ------------------------------------------------------------ the three bands


def test_high_mastery_extends_the_interval() -> None:
    result = review(90.0, ease=2.5, interval=4)
    assert result.band == "extended"
    assert result.interval_days == 10
    assert result.ease_factor == pytest.approx(2.5)


def test_low_mastery_resets_the_interval_and_decays_ease() -> None:
    result = review(40.0, ease=2.5, interval=30)
    assert result.band == "reset"
    assert result.interval_days == INITIAL_INTERVAL_DAYS
    assert result.ease_factor == pytest.approx(2.3)


def test_the_middle_band_holds_everything_where_it_is() -> None:
    """PRD 9.2 names no rule between the thresholds, so nothing changes and the
    concept comes round again on its current spacing (D-015)."""
    result = review(70.0, ease=2.5, interval=12)
    assert result.band == "held"
    assert result.interval_days == 12
    assert result.ease_factor == pytest.approx(2.5)


@pytest.mark.parametrize(
    ("mastery", "expected"),
    [
        (100.0, "extended"),
        (80.0, "extended"),  # the boundary is inclusive
        (79.9, "held"),
        (60.0, "held"),  # low band is strictly below
        (59.9, "reset"),
        (0.0, "reset"),
    ],
)
def test_the_band_boundaries_are_where_the_spec_puts_them(mastery: float, expected: str) -> None:
    assert review(mastery).band == expected


# -------------------------------------------------------------- ease decay


def test_ease_only_decays_never_recovers() -> None:
    """This variant differs from SM-2 proper, deliberately: a concept once
    failed keeps its reduced ease."""
    config = settings()
    after_failure = review(30.0, ease=2.5, config=config)
    after_success = review(95.0, ease=after_failure.ease_factor, config=config)

    assert after_failure.ease_factor == pytest.approx(2.3)
    assert after_success.ease_factor == pytest.approx(2.3)


def test_ease_bottoms_out_at_the_configured_floor() -> None:
    """ease 触底 1.3 — BACKLOG C2 acceptance."""
    config = settings()
    ease = 2.5
    for _ in range(20):
        ease = review(0.0, ease=ease, config=config).ease_factor

    assert ease == pytest.approx(1.3)


def test_the_floor_holds_exactly_rather_than_overshooting() -> None:
    """2.5 minus six penalties of 0.2 is 1.3; a seventh must not reach 1.1."""
    config = settings()
    ease = 1.4
    assert review(0.0, ease=ease, config=config).ease_factor == pytest.approx(1.3)
    assert review(0.0, ease=1.3, config=config).ease_factor == pytest.approx(1.3)


def test_a_held_review_does_not_touch_ease() -> None:
    assert review(70.0, ease=1.9).ease_factor == pytest.approx(1.9)


# ------------------------------------------------------------- the starvation


def test_a_concept_at_minimum_ease_still_spaces_out() -> None:
    """The failure this rounding choice exists to prevent: with floor or round,
    1 * 1.3 stays 1 and the learner reviews it daily forever while passing."""
    config = settings()
    interval = INITIAL_INTERVAL_DAYS
    intervals = [interval]
    for _ in range(5):
        interval = review(95.0, ease=1.3, interval=interval, config=config).interval_days
        intervals.append(interval)

    assert intervals == sorted(intervals)
    assert len(set(intervals)) == len(intervals), f"interval stopped growing: {intervals}"


def test_every_extension_moves_by_at_least_a_day() -> None:
    config = settings()
    for ease in (1.3, 1.5, 2.0, 2.5):
        for interval in (1, 2, 5, 30):
            result = review(95.0, ease=ease, interval=interval, config=config)
            assert result.interval_days > interval, f"stuck at ease={ease} interval={interval}"


# ----------------------------------------------------------------- over time


def test_consecutive_passes_space_the_concept_out() -> None:
    """连续通过 — BACKLOG C2 acceptance."""
    config = settings()
    interval, ease = INITIAL_INTERVAL_DAYS, 2.5
    seen = [interval]
    for _ in range(5):
        result = review(95.0, ease=ease, interval=interval, config=config)
        interval, ease = result.interval_days, result.ease_factor
        seen.append(interval)

    assert seen == [1, 3, 8, 20, 50, 125]
    assert ease == pytest.approx(2.5), "passing must not change ease in this variant"


def test_consecutive_failures_keep_it_daily_and_grind_ease_down() -> None:
    """连续失败 — BACKLOG C2 acceptance."""
    config = settings()
    interval, ease = 50, 2.5
    for _ in range(4):
        result = review(20.0, ease=ease, interval=interval, config=config)
        interval, ease = result.interval_days, result.ease_factor
        assert interval == INITIAL_INTERVAL_DAYS

    assert ease == pytest.approx(1.7)


def test_one_failure_undoes_a_long_run_of_spacing() -> None:
    """A concept 125 days out drops straight back to tomorrow — the point of
    the reset is that forgetting is not gradual."""
    assert review(30.0, ease=2.5, interval=125).interval_days == INITIAL_INTERVAL_DAYS


# ---------------------------------------------------------------- next_due_at


def test_next_due_is_the_interval_away_from_now() -> None:
    result = review(90.0, ease=2.5, interval=4)
    assert result.next_due_at == NOW + datetime.timedelta(days=10)


def test_next_due_keeps_the_timezone() -> None:
    result = review(70.0)
    assert result.next_due_at.tzinfo is not None
    assert result.next_due_at.utcoffset() == datetime.timedelta(0)


def test_a_naive_now_is_refused() -> None:
    """It would be stored as if it were in whatever timezone the session used,
    and surface months later as reviews arriving wrong for one region only."""
    with pytest.raises(ValueError, match="timezone-aware"):
        review(90.0, now=datetime.datetime(2026, 9, 16, 12, 0))


def test_a_non_utc_offset_is_accepted_and_preserved() -> None:
    """Callers should pass UTC, but a correct offset is not wrong — only a
    missing one is."""
    phnom_penh = datetime.timezone(datetime.timedelta(hours=7))
    moment = datetime.datetime(2026, 9, 16, 19, 0, tzinfo=phnom_penh)
    result = review(90.0, interval=1, now=moment)

    assert result.next_due_at == moment + datetime.timedelta(days=3)


# ------------------------------------------------------ everything is configured


def test_the_bands_come_from_configuration() -> None:
    lenient = settings(MASTERY_HIGH="50", MASTERY_LOW="20")
    assert review(55.0, config=lenient).band == "extended"
    assert review(30.0, config=lenient).band == "held"
    assert review(10.0, config=lenient).band == "reset"


def test_the_penalty_comes_from_configuration() -> None:
    harsh = settings(SM2_EASE_PENALTY="1.0")
    assert review(0.0, ease=2.5, config=harsh).ease_factor == pytest.approx(1.5)


def test_the_floor_comes_from_configuration() -> None:
    config = settings(SM2_EASE_MIN="2.0")
    assert review(0.0, ease=2.1, config=config).ease_factor == pytest.approx(2.0)


def test_the_initial_ease_comes_from_configuration() -> None:
    """Callers read this rather than letting the DDL default supply 2.5 (L-6)."""
    assert initial_ease_factor(settings()) == pytest.approx(2.5)
    assert initial_ease_factor(settings(SM2_EASE_INITIAL="3.0")) == pytest.approx(3.0)


# -------------------------------------------------------------- bad input


@pytest.mark.parametrize("mastery", [-0.1, 100.1])
def test_a_mastery_outside_the_scale_is_refused(mastery: float) -> None:
    with pytest.raises(ValueError, match="mastery must be within"):
        review(mastery)


def test_an_ease_below_one_is_refused() -> None:
    """Below 1.0 an interval shrinks on every success, which is not a slower
    schedule — it is a broken one."""
    with pytest.raises(ValueError, match="ease_factor must be at least"):
        review(90.0, ease=0.9)


@pytest.mark.parametrize("interval", [0, -5])
def test_a_non_positive_interval_is_refused(interval: int) -> None:
    with pytest.raises(ValueError, match="interval_days must be at least"):
        review(90.0, interval=interval)


def test_the_band_is_reported_for_diagnosis() -> None:
    """ "Why is this due again tomorrow" is unanswerable from the stored row."""
    assert review(95.0).band == "extended"
    assert review(20.0).ease_decayed is True
    assert review(95.0).ease_decayed is False
