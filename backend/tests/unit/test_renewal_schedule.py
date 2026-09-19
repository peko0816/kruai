"""The retry schedule, as arithmetic (BACKLOG D8b).

Small enough to test without a database, and worth testing that way: how many
chances a declined card gets before a subscription drops into grace is the kind
of number that is easy to be one out on, and being one out means either cutting
a paying learner off early or charging them a fourth time nobody agreed to.
"""

from __future__ import annotations

import datetime
from typing import Any

import pytest

from app.core.config import Settings
from app.services.subscriptions.renewal import next_attempt_at, retry_days

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

NOW = datetime.datetime(2026, 5, 4, 9, 0, tzinfo=datetime.UTC)


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {**_MINIMAL, **overrides}
    return Settings(_env_file=None, **values)


def test_the_shipped_schedule_is_the_configured_one() -> None:
    assert retry_days(settings()) == (1, 3)


def test_the_first_decline_is_retried_the_next_day() -> None:
    assert next_attempt_at(NOW, failures_so_far=1, settings=settings()) == NOW + datetime.timedelta(
        days=1
    )


def test_the_second_decline_waits_longer() -> None:
    assert next_attempt_at(NOW, failures_so_far=2, settings=settings()) == NOW + datetime.timedelta(
        days=3
    )


def test_the_schedule_runs_out_after_its_last_entry() -> None:
    """Three attempts in total with (1, 3): the due date, a day later, and
    three days after that. The fourth decline is where grace begins."""
    assert next_attempt_at(NOW, failures_so_far=3, settings=settings()) is None


@pytest.mark.parametrize("failures", [0, -1])
def test_a_charge_that_did_not_fail_has_no_retry(failures: int) -> None:
    """Called with a count that cannot have come from a decline, which would
    mean the caller lost track of what happened."""
    assert next_attempt_at(NOW, failures_so_far=failures, settings=settings()) is None


def test_an_empty_schedule_means_one_attempt_only() -> None:
    """A deployment that wants no retries says so, and the first decline goes
    straight to grace rather than looping."""
    config = settings(SUBSCRIPTION_AUTO_CHARGE_RETRY_DAYS="")

    assert retry_days(config) == ()
    assert next_attempt_at(NOW, failures_so_far=1, settings=config) is None


def test_a_longer_schedule_is_honoured_in_order() -> None:
    config = settings(SUBSCRIPTION_AUTO_CHARGE_RETRY_DAYS="1,3,7")

    assert next_attempt_at(NOW, failures_so_far=3, settings=config) == NOW + datetime.timedelta(
        days=7
    )
    assert next_attempt_at(NOW, failures_so_far=4, settings=config) is None


def test_a_retry_keeps_the_time_of_day() -> None:
    """Charging at nine in the morning and retrying at nine the next morning is
    what a learner's bank sees as the same recurring instruction."""
    retry = next_attempt_at(NOW, failures_so_far=1, settings=settings())

    assert retry is not None
    assert (retry.hour, retry.minute) == (9, 0)
