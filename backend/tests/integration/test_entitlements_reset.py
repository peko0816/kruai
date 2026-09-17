"""Clearing the daily counters, against a real database.

The guard that makes this idempotent is the interesting part: the reset is meant
to be called on every request, so calling it twice must do the work once. The
other thing worth pinning is what it leaves alone — realtime seconds are bought
by the month, and zeroing them here would delete something a learner paid for.
"""

from __future__ import annotations

import asyncio
import datetime
import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.core.db import create_engine, create_session_factory
from app.models.commerce import Entitlement
from app.models.users import User
from app.services.entitlements import (
    Entitlements,
    EntitlementsMissingError,
    QuotaReset,
    next_reset_at,
)

pytestmark = pytest.mark.integration

PHNOM_PENH = "Asia/Phnom_Penh"


def settings_for(url: URL, **overrides: str) -> Settings:
    values: dict[str, Any] = {
        "DATABASE_URL": url.render_as_string(hide_password=False),
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
        **overrides,
    }
    return Settings(_env_file=None, **values)


@pytest.fixture
async def engine(migrated_db: URL) -> AsyncEngine:
    return create_engine(settings_for(migrated_db))


async def provision(
    engine: AsyncEngine,
    *,
    reset_at: datetime.datetime,
    attempts_used: int = 0,
    tasks_used: int = 0,
    realtime_seconds: int = 0,
) -> uuid.UUID:
    factory = create_session_factory(engine)
    async with factory() as session:
        user = User(telegram_id=int(uuid.uuid4().int % 1_000_000_000))
        session.add(user)
        await session.flush()
        session.add(
            Entitlement(
                user_id=user.id,
                daily_attempts_used=attempts_used,
                daily_tasks_used=tasks_used,
                realtime_seconds_remaining=realtime_seconds,
                reset_at=reset_at,
            )
        )
        await session.commit()
        return user.id


async def stored(engine: AsyncEngine, user_id: uuid.UUID) -> Entitlement:
    factory = create_session_factory(engine)
    async with factory() as session:
        row = await session.get(Entitlement, user_id)
        assert row is not None
        return row


def resetter(engine: AsyncEngine, url: URL, **overrides: str) -> QuotaReset:
    return QuotaReset(
        session_factory=create_session_factory(engine), settings=settings_for(url, **overrides)
    )


NOW = datetime.datetime(2026, 9, 17, 12, 0, tzinfo=datetime.UTC)


# ------------------------------------------------------------- when it is due


async def test_a_due_reset_clears_the_daily_counters(engine: AsyncEngine, migrated_db: URL) -> None:
    service = resetter(engine, migrated_db)
    user_id = await provision(
        engine, reset_at=NOW - datetime.timedelta(hours=1), attempts_used=7, tasks_used=3
    )

    outcome = await service.reset_if_due(user_id, timezone=PHNOM_PENH, now=NOW)

    row = await stored(engine, user_id)
    assert outcome.performed is True
    assert row.daily_attempts_used == 0
    assert row.daily_tasks_used == 0
    await engine.dispose()


async def test_the_new_reset_time_is_the_next_local_midnight(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    config = settings_for(migrated_db)
    service = resetter(engine, migrated_db)
    user_id = await provision(engine, reset_at=NOW - datetime.timedelta(hours=1))

    outcome = await service.reset_if_due(user_id, timezone=PHNOM_PENH, now=NOW)

    assert outcome.next_reset_at == next_reset_at(NOW, timezone=PHNOM_PENH, settings=config)
    assert (await stored(engine, user_id)).reset_at == outcome.next_reset_at
    await engine.dispose()


async def test_a_reset_that_is_not_yet_due_does_nothing(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    service = resetter(engine, migrated_db)
    future = NOW + datetime.timedelta(hours=5)
    user_id = await provision(engine, reset_at=future, attempts_used=7)

    outcome = await service.reset_if_due(user_id, timezone=PHNOM_PENH, now=NOW)

    assert outcome.performed is False
    assert outcome.next_reset_at == future, "the stored time is authoritative, not a fresh one"
    assert (await stored(engine, user_id)).daily_attempts_used == 7
    await engine.dispose()


async def test_a_reset_exactly_on_the_boundary_fires(engine: AsyncEngine, migrated_db: URL) -> None:
    service = resetter(engine, migrated_db)
    user_id = await provision(engine, reset_at=NOW, attempts_used=4)

    assert (await service.reset_if_due(user_id, timezone=PHNOM_PENH, now=NOW)).performed is True
    await engine.dispose()


# ---------------------------------------------------------------- idempotence


async def test_calling_it_twice_resets_once(engine: AsyncEngine, migrated_db: URL) -> None:
    """It is meant to be called on every request, so the second call must be a
    no-op rather than pushing the reset time out by another day."""
    service = resetter(engine, migrated_db)
    user_id = await provision(engine, reset_at=NOW - datetime.timedelta(hours=1))

    first = await service.reset_if_due(user_id, timezone=PHNOM_PENH, now=NOW)
    second = await service.reset_if_due(user_id, timezone=PHNOM_PENH, now=NOW)

    assert first.performed is True
    assert second.performed is False
    assert second.next_reset_at == first.next_reset_at
    await engine.dispose()


async def test_concurrent_resets_produce_exactly_one(engine: AsyncEngine, migrated_db: URL) -> None:
    """Ten requests arriving at the stroke of midnight must not each clear the
    counters in turn and lose whatever the others recorded in between."""
    service = resetter(engine, migrated_db)
    user_id = await provision(engine, reset_at=NOW - datetime.timedelta(hours=1))

    outcomes = await asyncio.gather(
        *(service.reset_if_due(user_id, timezone=PHNOM_PENH, now=NOW) for _ in range(10))
    )

    assert sum(1 for o in outcomes if o.performed) == 1
    await engine.dispose()


# ------------------------------------------------------- what it leaves alone


async def test_the_realtime_balance_survives_a_daily_reset(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    """Those seconds are bought by the month and carried. Zeroing them here
    would delete something a learner paid for."""
    service = resetter(engine, migrated_db)
    user_id = await provision(
        engine, reset_at=NOW - datetime.timedelta(hours=1), attempts_used=5, realtime_seconds=1800
    )

    await service.reset_if_due(user_id, timezone=PHNOM_PENH, now=NOW)

    row = await stored(engine, user_id)
    assert row.daily_attempts_used == 0
    assert row.realtime_seconds_remaining == 1800
    await engine.dispose()


async def test_one_learners_reset_leaves_another_alone(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    service = resetter(engine, migrated_db)
    due = await provision(engine, reset_at=NOW - datetime.timedelta(hours=1), attempts_used=5)
    other = await provision(engine, reset_at=NOW - datetime.timedelta(hours=1), attempts_used=9)

    await service.reset_if_due(due, timezone=PHNOM_PENH, now=NOW)

    assert (await stored(engine, due)).daily_attempts_used == 0
    assert (await stored(engine, other)).daily_attempts_used == 9
    await engine.dispose()


# ------------------------------------------------- reset then spend, in order


async def test_a_learner_who_hit_the_cap_can_spend_again_after_a_reset(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    """The sequence D3 will run: reset if due, then consume."""
    config_overrides = {"LIMIT_FREE_DAILY_ATTEMPTS": "3"}
    service = resetter(engine, migrated_db, **config_overrides)
    quota = Entitlements(
        session_factory=create_session_factory(engine),
        settings=settings_for(migrated_db, **config_overrides),
    )
    user_id = await provision(engine, reset_at=NOW - datetime.timedelta(hours=1), attempts_used=3)

    await service.reset_if_due(user_id, timezone=PHNOM_PENH, now=NOW)
    result = await quota.consume_attempt(user_id, plan="free")

    assert result.remaining == 2
    await engine.dispose()


# --------------------------------------------------------------- bad state


async def test_a_missing_row_is_reported_as_such(engine: AsyncEngine, migrated_db: URL) -> None:
    service = resetter(engine, migrated_db)

    with pytest.raises(EntitlementsMissingError):
        await service.reset_if_due(uuid.uuid4(), timezone=PHNOM_PENH, now=NOW)
    await engine.dispose()


async def test_an_unknown_timezone_fails_before_touching_the_row(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    """A learner whose zone cannot be resolved must not have their counters
    cleared with an unusable next reset time written beside them."""
    service = resetter(engine, migrated_db)
    user_id = await provision(engine, reset_at=NOW - datetime.timedelta(hours=1), attempts_used=6)

    with pytest.raises(ValueError, match="unknown timezone"):
        await service.reset_if_due(user_id, timezone="Not/AZone", now=NOW)

    assert (await stored(engine, user_id)).daily_attempts_used == 6
    await engine.dispose()


async def test_the_stored_reset_time_keeps_its_timezone(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    """TIMESTAMPTZ round-trips as UTC; a naive value would be read back in
    whatever the session timezone happened to be."""
    service = resetter(engine, migrated_db)
    user_id = await provision(engine, reset_at=NOW - datetime.timedelta(hours=1))

    await service.reset_if_due(user_id, timezone=PHNOM_PENH, now=NOW)

    stored_at = (await stored(engine, user_id)).reset_at
    assert stored_at.tzinfo is not None
    assert stored_at.utcoffset() == datetime.timedelta(0)
    await engine.dispose()


async def test_learners_in_different_zones_get_different_reset_times(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    service = resetter(engine, migrated_db)
    east = await provision(engine, reset_at=NOW - datetime.timedelta(hours=1))
    west = await provision(engine, reset_at=NOW - datetime.timedelta(hours=1))

    await service.reset_if_due(east, timezone="Pacific/Kiritimati", now=NOW)
    await service.reset_if_due(west, timezone="Pacific/Niue", now=NOW)

    assert (await stored(engine, east)).reset_at != (await stored(engine, west)).reset_at
    await engine.dispose()


async def test_a_reset_row_can_be_read_back_by_a_plain_query(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    """A scheduled sweep would select on reset_at, so the column has to be
    usable as a predicate after a reset writes it."""
    service = resetter(engine, migrated_db)
    user_id = await provision(engine, reset_at=NOW - datetime.timedelta(hours=1))
    await service.reset_if_due(user_id, timezone=PHNOM_PENH, now=NOW)

    factory = create_session_factory(engine)
    async with factory() as session:
        due_now = await session.execute(
            sa.select(sa.func.count()).select_from(Entitlement).where(Entitlement.reset_at <= NOW)
        )
        assert due_now.scalar_one() == 0
    await engine.dispose()
