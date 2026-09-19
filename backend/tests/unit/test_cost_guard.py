"""The cost ceiling's arithmetic (BACKLOG D10), and carry-forward L-1.

COST_ALERT_MULTIPLIER is the only float in the project that touches money. It
is a ratio rather than an amount, so R4 holds — but 45 x 1.5 is 67.5, and
whether 67 counts as over is a decision, not something float ordering should
make. These tests are that decision written down.

The two boundary cases the BACKLOG asks for are exactly 67 and exactly 68.
"""

from __future__ import annotations

import datetime
from typing import Any

import pytest

from app.core.config import Plan, Settings
from app.services.entitlements import (
    alert_threshold_usd_cents,
    is_over_threshold,
    month_start,
)
from app.services.entitlements.cost_guard import scaled_multiplier

_MINIMAL: dict[str, Any] = {
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


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {**_MINIMAL, **overrides}
    return Settings(_env_file=None, **values)


# ------------------------------------------------------------------ the L-1 case


def test_the_threshold_rounds_up_rather_than_letting_a_float_decide() -> None:
    """Basic's cap is 45 and the multiplier is 1.5, so the line is 67.5.

    It has to fall on an integer somewhere, and up is the reading of "past 1.5
    times the cap": 67 is not past 67.5.
    """
    assert alert_threshold_usd_cents("basic", settings=settings()) == 68


@pytest.mark.parametrize(
    ("spend", "throttled"),
    [(66, False), (67, False), (68, True), (69, True)],
)
def test_the_boundary_is_where_it_was_decided_to_be(spend: int, throttled: bool) -> None:
    """The two cases the BACKLOG entry names: exactly 67 and exactly 68."""
    assert is_over_threshold(spend, plan="basic", settings=settings()) is throttled


def test_every_plan_gets_its_own_threshold() -> None:
    """PRD 11.2's caps are 15, 45 and 230."""
    config = settings()

    assert alert_threshold_usd_cents("free", settings=config) == 23  # 22.5 -> 23
    assert alert_threshold_usd_cents("basic", settings=config) == 68  # 67.5 -> 68
    assert alert_threshold_usd_cents("pro", settings=config) == 345  # exact


def test_a_threshold_that_lands_exactly_on_an_integer_is_not_nudged() -> None:
    """230 x 1.5 is 345 exactly; rounding up must not make it 346."""
    assert alert_threshold_usd_cents("pro", settings=settings()) == 345
    assert is_over_threshold(344, plan="pro", settings=settings()) is False
    assert is_over_threshold(345, plan="pro", settings=settings()) is True


# ---------------------------------------------------------------- the ratio


def test_the_ratio_becomes_an_integer_once() -> None:
    """Rounded here rather than at each comparison: a ratio that rounded
    differently per call site would make the threshold depend on who asked."""
    assert scaled_multiplier(settings()) == 1500
    assert scaled_multiplier(settings(COST_ALERT_MULTIPLIER="1.25")) == 1250
    assert scaled_multiplier(settings(COST_ALERT_MULTIPLIER="2")) == 2000


@pytest.mark.parametrize(
    ("multiplier", "expected"),
    [
        ("1.0", 45),
        ("1.1", 50),  # 49.5 -> 50
        ("1.25", 57),  # 56.25 -> 57
        ("1.5", 68),
        ("2.0", 90),
        ("3.333", 150),  # 149.985 -> 150
    ],
)
def test_awkward_ratios_still_land_on_an_integer(multiplier: str, expected: int) -> None:
    """None of these is exactly representable as a float, and none of them is
    allowed to produce a threshold that drifts."""
    config = settings(COST_ALERT_MULTIPLIER=multiplier)

    assert alert_threshold_usd_cents("basic", settings=config) == expected


def test_a_ratio_just_over_one_is_not_truncated_back_to_one() -> None:
    """The rounding of the *ratio* matters as much as the rounding of the
    threshold, and for a subtler reason.

    1.001 is not representable in binary: the nearest float times a thousand is
    1000.9999999999999. Truncating that gives 1000 -- exactly one -- so an
    operator who asked for a whisker of headroom would get none at all, and a
    learner sitting on the cap would be throttled by a multiplier that says they
    should not be. Rounding gives 1001, and the threshold moves to 46 as asked.
    """
    config = settings(COST_ALERT_MULTIPLIER="1.001")

    assert scaled_multiplier(config) == 1001
    assert alert_threshold_usd_cents("basic", settings=config) == 46
    assert is_over_threshold(45, plan="basic", settings=config) is False


def test_a_multiplier_of_one_throttles_at_the_cap_itself() -> None:
    """The cap is the cap. An operator who wants no headroom says 1.0."""
    config = settings(COST_ALERT_MULTIPLIER="1.0")

    assert is_over_threshold(44, plan="basic", settings=config) is False
    assert is_over_threshold(45, plan="basic", settings=config) is True


def test_nothing_in_the_calculation_is_a_float() -> None:
    """R4's spirit: the ratio is converted once and every number after it is an
    int, so no comparison anywhere depends on binary floating point."""
    threshold = alert_threshold_usd_cents("basic", settings=settings())

    assert isinstance(threshold, int)
    assert isinstance(scaled_multiplier(settings()), int)


@pytest.mark.parametrize("plan", ["free", "basic", "pro"])
def test_a_learner_who_has_spent_nothing_is_never_throttled(plan: Plan) -> None:
    assert is_over_threshold(0, plan=plan, settings=settings()) is False


def test_a_zero_cap_throttles_at_the_first_cent() -> None:
    """A deployment that sets a cap of 0 means "nothing"; the threshold is 0 and
    any spend is already past it. Spending nothing still is not."""
    config = settings(COST_CAP_BASIC_USD_CENTS_MONTHLY="0")

    assert alert_threshold_usd_cents("basic", settings=config) == 0
    assert is_over_threshold(0, plan="basic", settings=config) is True


# ----------------------------------------------------------------- the month


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (
            datetime.datetime(2026, 5, 4, 9, 30, tzinfo=datetime.UTC),
            datetime.datetime(2026, 5, 1, tzinfo=datetime.UTC),
        ),
        (
            datetime.datetime(2026, 1, 1, 0, 0, tzinfo=datetime.UTC),
            datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
        ),
        (
            datetime.datetime(2026, 12, 31, 23, 59, tzinfo=datetime.UTC),
            datetime.datetime(2026, 12, 1, tzinfo=datetime.UTC),
        ),
    ],
)
def test_the_month_starts_at_midnight_utc_on_the_first(
    now: datetime.datetime, expected: datetime.datetime
) -> None:
    """UTC rather than the learner's own month: the cap is a statement about
    our bill, and one bill covers people in several timezones. The daily
    allowance is the other way round (C5), because that one is about their day.
    """
    assert month_start(now) == expected
