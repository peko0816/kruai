"""Forgetting to record a cost has to be loud.

CLAUDE.md section 8 says an unledgered call counts as not having happened, which
only holds if the omission is noisy. These tests cover the guard and the input
validation; test_cost_ledger.py under integration/ covers what actually reaches
the table.

Every session factory here explodes if called. That is deliberate — it proves
the guard fires before any database work, so a forgotten record() cannot leave a
half-written row behind.
"""

from __future__ import annotations

import uuid
from typing import Any, NoReturn

import pytest

from app.core.config import Settings
from app.services.cost_ledger import CostLedger, PendingEntry, UnledgeredCallError

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


def never_called() -> NoReturn:
    raise AssertionError("the ledger opened a session when it should not have")


def ledger(**overrides: str) -> CostLedger:
    return CostLedger(session_factory=never_called, settings=settings(**overrides))


# ------------------------------------------------------------------ the guard


async def test_a_call_that_records_nothing_raises() -> None:
    """BACKLOG B6 acceptance."""
    with pytest.raises(UnledgeredCallError):
        async with ledger().external_call(provider="fake", ref="attempt"):
            pass


async def test_the_error_says_which_call_and_how_to_fix_it() -> None:
    with pytest.raises(UnledgeredCallError) as exc:
        async with ledger().external_call(provider="azure", ref="attempt"):
            pass

    message = str(exc.value)
    assert "azure" in message
    assert "attempt" in message
    assert "entry.record" in message


async def test_the_guard_can_be_turned_off_but_still_warns() -> None:
    """CONFIG_REFERENCE says dev and CI must leave it on; staging might not."""
    async with ledger(COST_LEDGER_REQUIRED="false").external_call(provider="fake", ref="attempt"):
        pass


async def test_recording_satisfies_the_guard() -> None:
    """The session factory would explode, so reaching persistence is visible."""
    with pytest.raises(AssertionError, match="opened a session"):
        async with ledger().external_call(provider="fake", ref="attempt") as entry:
            entry.record(unit="calls", quantity=1, cost_usd_cents=1)


async def test_an_exception_inside_the_block_is_not_masked() -> None:
    """A failing call is already loud; the guard must not replace that error."""
    with pytest.raises(ZeroDivisionError):
        async with ledger().external_call(provider="fake", ref="attempt"):
            raise ZeroDivisionError("the provider blew up")


async def test_a_failing_call_that_recorded_nothing_is_not_reported_as_unledgered() -> None:
    """Nothing was spent, so there is nothing to account for."""
    with pytest.raises(ValueError, match="provider said no"):
        async with ledger().external_call(provider="fake", ref="attempt"):
            raise ValueError("provider said no")


# -------------------------------------------------------- what may be recorded


@pytest.mark.parametrize("cost", [1.5, "1", True])
def test_non_integer_costs_are_refused(cost: Any) -> None:
    """The column is _usd_cents, so R4 applies — and bool passes isinstance(int)."""
    entry = PendingEntry(provider="fake", ref="attempt")
    with pytest.raises(TypeError, match="must be int"):
        entry.record(unit="calls", quantity=1, cost_usd_cents=cost)


def test_negative_costs_are_refused() -> None:
    """A negative would silently pull down every aggregate reading this table."""
    entry = PendingEntry(provider="fake", ref="attempt")
    with pytest.raises(ValueError, match="cost_usd_cents must be >= 0"):
        entry.record(unit="calls", quantity=1, cost_usd_cents=-1)


def test_negative_quantities_are_refused() -> None:
    entry = PendingEntry(provider="fake", ref="attempt")
    with pytest.raises(ValueError, match="quantity must be >= 0"):
        entry.record(unit="seconds", quantity=-4, cost_usd_cents=0)


def test_a_zero_cost_call_still_counts_as_recorded() -> None:
    """FakeTTS reports 0 for a short line; that is an answer, not an omission."""
    entry = PendingEntry(provider="fake", ref="content_production")
    entry.record(unit="calls", quantity=1, cost_usd_cents=0)

    assert len(entry.costs) == 1
    assert entry.total_usd_cents == 0


def test_several_costs_can_be_recorded_for_one_call() -> None:
    """An LLM call bills input and output separately."""
    entry = PendingEntry(provider="openai", ref="attempt")
    entry.record(unit="tokens", quantity=1200, cost_usd_cents=2)
    entry.record(unit="tokens", quantity=300, cost_usd_cents=3)

    assert entry.total_usd_cents == 5


def test_the_user_is_optional() -> None:
    """Pack builds are not attributable to anyone (PRD 11.3)."""
    assert PendingEntry(provider="fake", ref="content_production").user_id is None


def test_a_user_can_be_attached() -> None:
    user = uuid.uuid4()
    assert PendingEntry(provider="fake", ref="attempt", user_id=user).user_id == user
