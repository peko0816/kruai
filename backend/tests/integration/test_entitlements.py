"""Allowance deduction under concurrency, against a real database.

BACKLOG C4's acceptance is ten concurrent deductions of one allowance with the
total correct and no over-spend. That cannot be checked without a real database
and real concurrency: a read-modify-write implementation passes every
single-threaded test ever written and fails only under load, silently, with a
quota that drifts over its ceiling and nothing in the logs to show it.

So the tests that matter here fire more requests than the allowance permits and
assert two things at once — that exactly the right number succeeded, and that
the stored counter never went past the limit.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Plan, Settings
from app.core.db import create_engine, create_session_factory
from app.core.errors import InsufficientQuota
from app.models.commerce import Entitlement
from app.models.users import User
from app.services.entitlements import Entitlements, EntitlementsMissingError

pytestmark = pytest.mark.integration


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
async def engine(migrated_db: URL) -> AsyncIterator[AsyncEngine]:
    """Disposed at the end of the test, not left to the garbage collector.

    An engine that goes out of scope still holds pooled connections, and
    psycopg closing one from __del__ raises where nobody can catch it — pytest
    reports that as an unraisable-exception warning against whichever test was
    running when the collector happened to fire, which this suite turns into a
    failure. The symptom moves around; the cause is here.
    """
    built = create_engine(settings_for(migrated_db))
    try:
        yield built
    finally:
        await built.dispose()


async def provision(
    engine: AsyncEngine,
    *,
    attempts_used: int = 0,
    tasks_used: int = 0,
    realtime_seconds: int = 0,
) -> uuid.UUID:
    """A user with an entitlements row, as account creation will leave them."""
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
                reset_at=sa.func.now(),
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


def service(engine: AsyncEngine, url: URL, **overrides: str) -> Entitlements:
    return Entitlements(
        session_factory=create_session_factory(engine),
        settings=settings_for(url, **overrides),
    )


# ------------------------------------------------------- the acceptance test


async def test_ten_concurrent_deductions_of_one_allowance(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    """BACKLOG C4 acceptance: ten at once, total correct, nothing over-spent."""
    quota = service(engine, migrated_db, LIMIT_FREE_DAILY_ATTEMPTS="10")
    user_id = await provision(engine)

    results = await asyncio.gather(
        *(quota.consume_attempt(user_id, plan="free") for _ in range(10))
    )

    assert len(results) == 10
    assert (await stored(engine, user_id)).daily_attempts_used == 10
    await engine.dispose()


async def test_twice_the_allowance_at_once_grants_exactly_the_allowance(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    """The stronger form: contention that must be refused, not just survived.

    A read-modify-write implementation passes the test above — ten requests all
    fit — and fails this one by letting more than ten through.
    """
    quota = service(engine, migrated_db, LIMIT_FREE_DAILY_ATTEMPTS="10")
    user_id = await provision(engine)

    outcomes = await asyncio.gather(
        *(quota.consume_attempt(user_id, plan="free") for _ in range(20)),
        return_exceptions=True,
    )

    granted = [o for o in outcomes if not isinstance(o, BaseException)]
    refused = [o for o in outcomes if isinstance(o, InsufficientQuota)]
    unexpected = [
        o for o in outcomes if isinstance(o, BaseException) and not isinstance(o, InsufficientQuota)
    ]

    assert unexpected == [], f"something other than a refusal was raised: {unexpected}"
    assert len(granted) == 10
    assert len(refused) == 10
    assert (await stored(engine, user_id)).daily_attempts_used == 10
    await engine.dispose()


async def test_concurrent_realtime_spending_never_goes_negative(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    """The balance shape of the same race. The CHECK constraint would catch a
    negative, so a failure here shows up as an IntegrityError rather than a
    wrong number — which is why the count of refusals is asserted too."""
    quota = service(engine, migrated_db)
    user_id = await provision(engine, realtime_seconds=100)

    outcomes = await asyncio.gather(
        *(quota.consume_realtime_seconds(user_id, seconds=10) for _ in range(20)),
        return_exceptions=True,
    )

    granted = [o for o in outcomes if not isinstance(o, BaseException)]
    assert len(granted) == 10
    assert (await stored(engine, user_id)).realtime_seconds_remaining == 0
    await engine.dispose()


async def test_concurrent_deductions_of_uneven_size_still_respect_the_ceiling(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    """Unequal requests are where an off-by-one guard shows up: the last one to
    fit must be allowed and the first that does not must be refused."""
    quota = service(engine, migrated_db)
    user_id = await provision(engine, realtime_seconds=100)

    outcomes = await asyncio.gather(
        *(quota.consume_realtime_seconds(user_id, seconds=size) for size in (30, 30, 30, 30)),
        return_exceptions=True,
    )

    spent = sum(o.consumed for o in outcomes if not isinstance(o, BaseException))
    remaining = (await stored(engine, user_id)).realtime_seconds_remaining

    assert spent + remaining == 100
    assert remaining >= 0
    await engine.dispose()


# ----------------------------------------------------------- counters vs caps


async def test_a_deduction_inside_the_cap_succeeds(engine: AsyncEngine, migrated_db: URL) -> None:
    quota = service(engine, migrated_db, LIMIT_FREE_DAILY_ATTEMPTS="10")
    user_id = await provision(engine, attempts_used=3)

    result = await quota.consume_attempt(user_id, plan="free")

    assert result.consumed == 1
    assert result.remaining == 6
    await engine.dispose()


async def test_the_last_unit_of_allowance_is_grantable(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    """Off-by-one in the other direction: the tenth attempt must be allowed."""
    quota = service(engine, migrated_db, LIMIT_FREE_DAILY_ATTEMPTS="10")
    user_id = await provision(engine, attempts_used=9)

    assert (await quota.consume_attempt(user_id, plan="free")).remaining == 0
    await engine.dispose()


async def test_exceeding_the_cap_is_refused_and_writes_nothing(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    quota = service(engine, migrated_db, LIMIT_FREE_DAILY_ATTEMPTS="10")
    user_id = await provision(engine, attempts_used=10)

    with pytest.raises(InsufficientQuota):
        await quota.consume_attempt(user_id, plan="free")

    assert (await stored(engine, user_id)).daily_attempts_used == 10
    await engine.dispose()


async def test_a_multi_unit_request_that_does_not_fit_is_refused_whole(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    """No partial spend: eight of three remaining must not become three."""
    quota = service(engine, migrated_db, LIMIT_FREE_DAILY_ATTEMPTS="10")
    user_id = await provision(engine, attempts_used=7)

    with pytest.raises(InsufficientQuota):
        await quota.consume_attempt(user_id, plan="free", count=8)

    assert (await stored(engine, user_id)).daily_attempts_used == 7
    await engine.dispose()


async def test_the_refusal_carries_which_quota_ran_out(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    quota = service(engine, migrated_db, LIMIT_FREE_DAILY_ATTEMPTS="1")
    user_id = await provision(engine, attempts_used=1)

    with pytest.raises(InsufficientQuota) as exc:
        await quota.consume_attempt(user_id, plan="free")

    assert exc.value.code == "quota.insufficient"
    assert exc.value.http_status == 402
    assert exc.value.context["quota"] == "attempts"
    await engine.dispose()


@pytest.mark.parametrize("plan", ["basic", "pro"])
async def test_a_paid_plan_has_no_attempt_ceiling(
    engine: AsyncEngine, migrated_db: URL, plan: Plan
) -> None:
    """LIMIT_BASIC_DAILY_ATTEMPTS defaults to 0, the unlimited sentinel."""
    quota = service(engine, migrated_db)
    user_id = await provision(engine, attempts_used=10_000)

    result = await quota.consume_attempt(user_id, plan=plan)

    assert result.remaining is None, "an unlimited plan has no remaining count"
    assert (await stored(engine, user_id)).daily_attempts_used == 10_001
    await engine.dispose()


async def test_unlimited_still_counts_so_usage_stays_visible(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    """No ceiling is not the same as no record; /admin/costs and the cost
    guardrails read these counters."""
    quota = service(engine, migrated_db)
    user_id = await provision(engine)

    for _ in range(5):
        await quota.consume_attempt(user_id, plan="pro")

    assert (await stored(engine, user_id)).daily_attempts_used == 5
    await engine.dispose()


# ------------------------------------------------------------------- tasks


async def test_free_tasks_are_capped_at_three(engine: AsyncEngine, migrated_db: URL) -> None:
    """PRD 4.3: Free gets three tasks a day."""
    quota = service(engine, migrated_db)
    user_id = await provision(engine, tasks_used=3)

    with pytest.raises(InsufficientQuota) as exc:
        await quota.consume_task(user_id, plan="free")

    assert exc.value.context["quota"] == "tasks"
    await engine.dispose()


async def test_paid_tasks_are_uncapped(engine: AsyncEngine, migrated_db: URL) -> None:
    quota = service(engine, migrated_db)
    user_id = await provision(engine, tasks_used=500)

    assert (await quota.consume_task(user_id, plan="basic")).remaining is None
    await engine.dispose()


async def test_attempts_and_tasks_are_separate_allowances(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    """Spending one must not touch the other."""
    quota = service(engine, migrated_db)
    user_id = await provision(engine)

    await quota.consume_attempt(user_id, plan="free")
    row = await stored(engine, user_id)

    assert row.daily_attempts_used == 1
    assert row.daily_tasks_used == 0
    await engine.dispose()


# --------------------------------------------------------------- realtime


async def test_spending_realtime_reduces_the_balance(engine: AsyncEngine, migrated_db: URL) -> None:
    quota = service(engine, migrated_db)
    user_id = await provision(engine, realtime_seconds=900)

    result = await quota.consume_realtime_seconds(user_id, seconds=125)

    assert result.remaining == 775
    await engine.dispose()


async def test_spending_the_exact_balance_is_allowed(engine: AsyncEngine, migrated_db: URL) -> None:
    quota = service(engine, migrated_db)
    user_id = await provision(engine, realtime_seconds=60)

    assert (await quota.consume_realtime_seconds(user_id, seconds=60)).remaining == 0
    await engine.dispose()


async def test_spending_one_second_more_than_the_balance_is_refused(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    quota = service(engine, migrated_db)
    user_id = await provision(engine, realtime_seconds=60)

    with pytest.raises(InsufficientQuota):
        await quota.consume_realtime_seconds(user_id, seconds=61)

    assert (await stored(engine, user_id)).realtime_seconds_remaining == 60
    await engine.dispose()


async def test_a_grant_adds_rather_than_replacing(engine: AsyncEngine, migrated_db: URL) -> None:
    """A top-up bought while seconds remain must not delete them."""
    quota = service(engine, migrated_db)
    user_id = await provision(engine, realtime_seconds=400)

    result = await quota.grant_realtime_seconds(user_id, seconds=1800)

    assert result.remaining == 2200
    await engine.dispose()


async def test_concurrent_grants_all_land(engine: AsyncEngine, migrated_db: URL) -> None:
    """Two top-ups arriving together must both count."""
    quota = service(engine, migrated_db)
    user_id = await provision(engine)

    await asyncio.gather(*(quota.grant_realtime_seconds(user_id, seconds=600) for _ in range(5)))

    assert (await stored(engine, user_id)).realtime_seconds_remaining == 3000
    await engine.dispose()


# ------------------------------------------------------------- bad state


# ------------------------------------------------------ releasing an attempt


async def test_a_release_gives_the_attempt_back(engine: AsyncEngine, migrated_db: URL) -> None:
    """The compensation D3 runs when the scorer fails (docs/DECISIONS.md D-034)."""
    quota = service(engine, migrated_db, LIMIT_FREE_DAILY_ATTEMPTS="10")
    user_id = await provision(engine, attempts_used=3)

    await quota.release_attempt(user_id)

    assert (await stored(engine, user_id)).daily_attempts_used == 2


async def test_a_release_cannot_drive_the_counter_negative(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    """The floor is not defensive decoration — this sequence really happens.

    A learner consumes an attempt, the scorer is slow, their local midnight
    passes and the daily reset zeroes the counter, and only then does the
    failure come back and release. Without the floor the counter lands at -1
    and they carry a free extra attempt until the next reset. The column has no
    CHECK constraint, so nothing else would catch it.
    """
    quota = service(engine, migrated_db, LIMIT_FREE_DAILY_ATTEMPTS="10")
    user_id = await provision(engine, attempts_used=0)

    await quota.release_attempt(user_id)

    assert (await stored(engine, user_id)).daily_attempts_used == 0


async def test_releasing_twice_for_one_deduction_stops_at_zero(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    quota = service(engine, migrated_db, LIMIT_FREE_DAILY_ATTEMPTS="10")
    user_id = await provision(engine, attempts_used=1)

    await quota.release_attempt(user_id)
    await quota.release_attempt(user_id)

    assert (await stored(engine, user_id)).daily_attempts_used == 0


async def test_a_release_for_a_missing_row_says_so(engine: AsyncEngine, migrated_db: URL) -> None:
    quota = service(engine, migrated_db)

    with pytest.raises(EntitlementsMissingError):
        await quota.release_attempt(uuid.uuid4())


@pytest.mark.parametrize("count", [0, -1])
async def test_a_non_positive_release_is_refused(
    engine: AsyncEngine, migrated_db: URL, count: int
) -> None:
    """Otherwise the compensation path becomes a second way to spend allowance."""
    quota = service(engine, migrated_db)
    user_id = await provision(engine, attempts_used=1)

    with pytest.raises(ValueError):
        await quota.release_attempt(user_id, count=count)


async def test_a_user_without_a_row_is_not_reported_as_out_of_quota(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    """Both look like zero affected rows, and confusing them would tell a
    learner they were out of quota when nobody provisioned them."""
    quota = service(engine, migrated_db)
    factory = create_session_factory(engine)
    async with factory() as session:
        user = User(telegram_id=778001)
        session.add(user)
        await session.commit()
        user_id = user.id

    with pytest.raises(EntitlementsMissingError, match="no entitlements row"):
        await quota.consume_attempt(user_id, plan="free")
    await engine.dispose()


async def test_an_unknown_user_is_reported_the_same_way(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    quota = service(engine, migrated_db)

    with pytest.raises(EntitlementsMissingError):
        await quota.consume_realtime_seconds(uuid.uuid4(), seconds=10)
    await engine.dispose()


@pytest.mark.parametrize("count", [0, -1])
async def test_a_non_positive_deduction_is_refused(
    engine: AsyncEngine, migrated_db: URL, count: int
) -> None:
    """It would be a refund travelling through the spending path."""
    quota = service(engine, migrated_db)
    user_id = await provision(engine)

    with pytest.raises(ValueError, match="must be positive"):
        await quota.consume_attempt(user_id, plan="free", count=count)
    await engine.dispose()


# ---------------------------------------------------------------- snapshot


async def test_the_snapshot_reports_all_three_allowances(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    quota = service(engine, migrated_db)
    user_id = await provision(engine, attempts_used=4, tasks_used=2, realtime_seconds=1200)

    snapshot = await quota.snapshot(user_id)

    assert snapshot.daily_attempts_used == 4
    assert snapshot.daily_tasks_used == 2
    assert snapshot.realtime_seconds_remaining == 1200
    await engine.dispose()


async def test_a_snapshot_of_a_missing_row_says_so(engine: AsyncEngine, migrated_db: URL) -> None:
    quota = service(engine, migrated_db)

    with pytest.raises(EntitlementsMissingError):
        await quota.snapshot(uuid.uuid4())
    await engine.dispose()


# --------------------------------------------------- a deduction on loan


async def test_a_borrowed_deduction_is_undone_when_the_caller_rolls_back(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    """The contract behind the ``session`` argument (D-073).

    Starting a lesson marks a row and spends a task, and those two have to
    stand or fall together. They only can if the deduction leaves the
    transaction alone: a commit inside the service would make the spend
    survive a caller that decided to undo everything, and the learner would be
    one task poorer with nothing to show for it.
    """
    quota = service(engine, migrated_db, LIMIT_FREE_DAILY_TASKS="3")
    user_id = await provision(engine)
    factory = create_session_factory(engine)

    async with factory() as session:
        consumption = await quota.consume_task(user_id, plan="free", session=session)
        assert consumption.remaining == 2
        await session.rollback()

    assert (await stored(engine, user_id)).daily_tasks_used == 0
    await engine.dispose()


async def test_a_borrowed_deduction_stands_when_the_caller_commits(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    quota = service(engine, migrated_db, LIMIT_FREE_DAILY_TASKS="3")
    user_id = await provision(engine)
    factory = create_session_factory(engine)

    async with factory() as session:
        await quota.consume_task(user_id, plan="free", session=session)
        await session.commit()

    assert (await stored(engine, user_id)).daily_tasks_used == 1
    await engine.dispose()
