"""Prices and billing periods (BACKLOG D8a).

The money half of the acceptance: USD has two decimal places and KHR has none,
and the whole point of ``amount_minor`` is that one table can hold both without
either being read as the other. Nothing here is a float.

The period half is calendar arithmetic. Thirty days is not a month, and a
subscription billed every thirty days walks backwards through the calendar
until it lands on a day the learner never agreed to.
"""

from __future__ import annotations

import datetime
from typing import Any

import pytest

from app.core.config import Plan, Settings
from app.core.money import format_money
from app.services.entitlements.pricing import (
    BILLING_PERIODS,
    PURCHASABLE_PLANS,
    BillingPeriod,
    UnpricedError,
    grace_until,
    parse_price_table,
    period_end,
    price_for,
)

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


# --------------------------------------------------------------- the price list


def test_the_shipped_prices_are_the_ones_in_the_prd() -> None:
    """PRD 4.3: Basic $1.99/month or $18/year, Pro $5.99/month or $54/year."""
    config = settings()

    assert price_for("basic", "monthly", "USD", settings=config).amount_minor == 199
    assert price_for("basic", "yearly", "USD", settings=config).amount_minor == 1800
    assert price_for("pro", "monthly", "USD", settings=config).amount_minor == 599
    assert price_for("pro", "yearly", "USD", settings=config).amount_minor == 5400


def test_a_price_carries_its_currency_s_decimal_places() -> None:
    money = price_for("basic", "monthly", "USD", settings=settings())

    assert money.currency == "USD"
    assert money.currency_minor_units == 2
    assert format_money(money) == "$1.99"


def test_a_riel_price_has_no_decimal_places() -> None:
    """KHR has no minor unit at all. An amount_minor of 8000 is ៛8000, and
    reading it as cents would be off by a factor of a hundred."""
    config = settings(SUPPORTED_CURRENCIES="USD,KHR", PRICE_BASIC_MONTHLY="USD:199,KHR:8000")

    money = price_for("basic", "monthly", "KHR", settings=config)

    assert money.amount_minor == 8000
    assert money.currency_minor_units == 0
    assert format_money(money) == "៛8,000"


def test_the_two_currencies_are_not_the_same_number_scaled() -> None:
    """A guard against the tempting bug: converting between them by 10**n."""
    config = settings(SUPPORTED_CURRENCIES="USD,KHR", PRICE_BASIC_MONTHLY="USD:199,KHR:8000")

    dollars = price_for("basic", "monthly", "USD", settings=config)
    riel = price_for("basic", "monthly", "KHR", settings=config)

    assert dollars.amount_minor != riel.amount_minor
    assert dollars.currency_minor_units != riel.currency_minor_units


@pytest.mark.parametrize("plan", PURCHASABLE_PLANS)
@pytest.mark.parametrize("period", BILLING_PERIODS)
def test_every_purchasable_combination_is_priced(plan: Plan, period: BillingPeriod) -> None:
    """The self-check refuses to boot without this; here it is as a unit test."""
    assert price_for(plan, period, "USD", settings=settings()).amount_minor > 0


def test_free_cannot_be_bought() -> None:
    with pytest.raises(ValueError, match="cannot be bought"):
        price_for("free", "monthly", "USD", settings=settings())


def test_an_unpriced_currency_is_refused_rather_than_guessed() -> None:
    config = settings(SUPPORTED_CURRENCIES="USD,KHR")

    with pytest.raises(UnpricedError, match="no KHR price"):
        price_for("basic", "monthly", "KHR", settings=config)


def test_the_yearly_price_is_the_discount_the_prd_describes() -> None:
    """About 25% off twelve months, which is why both periods exist."""
    config = settings()
    monthly = price_for("basic", "monthly", "USD", settings=config).amount_minor
    yearly = price_for("basic", "yearly", "USD", settings=config).amount_minor

    assert yearly < monthly * 12
    assert round((1 - yearly / (monthly * 12)) * 100) == 25


# ------------------------------------------------------------ parsing prices


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("USD:199", {"USD": 199}),
        ("USD:199,KHR:8000", {"USD": 199, "KHR": 8000}),
        (" usd : 199 , khr : 8000 ", {"USD": 199, "KHR": 8000}),
        ("USD:199,", {"USD": 199}),
    ],
)
def test_price_tables_parse(raw: str, expected: dict[str, int]) -> None:
    assert parse_price_table(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "199",
        "USD",
        "EUR:199",
        "USD:1.99",
        "USD:0",
        "USD:-199",
        "USD:199,USD:299",
    ],
)
def test_a_malformed_price_refuses_to_load(raw: str) -> None:
    """Every one of these is a deployment that would take the wrong amount, so
    none of them is allowed to start."""
    with pytest.raises(ValueError):
        parse_price_table(raw)


def test_a_float_price_is_refused_by_name() -> None:
    """1.99 in a config file means somebody thought in dollars, not in the
    smallest unit — the single most likely way to charge a hundredth."""
    with pytest.raises(ValueError, match="float money is a bug"):
        parse_price_table("USD:1.99")


# ---------------------------------------------------------------- the period


def utc(year: int, month: int, day: int) -> datetime.datetime:
    return datetime.datetime(year, month, day, 12, 0, tzinfo=datetime.UTC)


@pytest.mark.parametrize(
    ("start", "period", "expected"),
    [
        (utc(2026, 1, 15), "monthly", utc(2026, 2, 15)),
        (utc(2026, 12, 15), "monthly", utc(2027, 1, 15)),
        (utc(2026, 1, 15), "yearly", utc(2027, 1, 15)),
        # The month-end cases, which are the reason this is not += 30 days.
        (utc(2026, 1, 31), "monthly", utc(2026, 2, 28)),
        (utc(2028, 1, 31), "monthly", utc(2028, 2, 29)),
        (utc(2026, 3, 31), "monthly", utc(2026, 4, 30)),
        (utc(2028, 2, 29), "yearly", utc(2029, 2, 28)),
    ],
)
def test_a_period_ends_on_the_calendar(
    start: datetime.datetime, period: BillingPeriod, expected: datetime.datetime
) -> None:
    assert period_end(start, period) == expected


def test_a_month_is_not_thirty_days() -> None:
    """Billing every 30 days walks the date backwards through the year until it
    lands on one the learner never agreed to."""
    start = utc(2026, 1, 15)

    assert period_end(start, "monthly") - start != datetime.timedelta(days=30)


def test_a_period_keeps_the_time_of_day() -> None:
    """A subscription bought at 23:00 does not quietly expire at midnight."""
    start = datetime.datetime(2026, 5, 4, 23, 17, 3, tzinfo=datetime.UTC)

    ends = period_end(start, "monthly")

    assert (ends.hour, ends.minute, ends.second) == (23, 17, 3)


def test_a_naive_start_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        period_end(datetime.datetime(2026, 1, 15, 12), "monthly")


def test_grace_extends_past_the_period() -> None:
    """ARCHITECTURE 3.4: benefits continue for a few days after expiry."""
    config = settings(SUBSCRIPTION_GRACE_DAYS="3")
    ends = utc(2026, 2, 28)

    assert grace_until(ends, settings=config) == utc(2026, 3, 3)
