"""Allowance checks that cannot be raced.

CODING_STANDARDS section 8 forbids read-modify-write here, and the reason is
specific rather than stylistic. Between reading a counter and writing it back
there is a window, and ten concurrent requests from one learner all read the
same number, all decide there is room, and all write. The quota is exceeded, no
statement failed, and nothing in the logs says so.

So the check and the write are the same statement. The guard lives in the WHERE
clause, and a row count of zero is the refusal:

    UPDATE entitlements SET daily_attempts_used = daily_attempts_used + 1
    WHERE user_id = ? AND daily_attempts_used + 1 <= limit

Two shapes of allowance, because the table holds both. Attempts and tasks are
counters that climb toward a plan limit and reset daily; realtime seconds are a
balance that is bought and spent down. The counters can be unlimited — a plan
limit of 0 means no ceiling — while a balance always has one.

This is the one domain service that touches the database. ARCHITECTURE section 1
puts entitlements in the pure-logic layer, and it cannot be: the decision and
the write have to be a single statement, so separating them would recreate
exactly the race the rule exists to prevent (docs/DECISIONS.md D-018).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Plan, Settings
from app.core.errors import InsufficientQuota
from app.core.logging import get_logger
from app.models.commerce import Entitlement

log = get_logger(__name__)

#: A plan limit of 0 means no ceiling (CONFIG_REFERENCE section 4).
UNLIMITED = 0


class EntitlementsMissingError(LookupError):
    """The user has no entitlements row.

    Distinct from InsufficientQuota because the two are indistinguishable from
    the row count alone, and confusing them would tell a learner they were out
    of quota when the truth is that nobody provisioned them.
    """


@dataclass(frozen=True)
class QuotaConsumption:
    """What a successful consume did."""

    consumed: int
    #: None when this plan has no ceiling on this quota.
    remaining: int | None


@dataclass(frozen=True)
class QuotaSnapshot:
    """Current allowance, for display only.

    Never gate on this. Reading it and then acting on the answer is the
    read-modify-write this module exists to avoid — by the time a caller decides,
    the number can already be stale. Only the consume methods are authoritative.
    """

    daily_attempts_used: int
    daily_tasks_used: int
    realtime_seconds_remaining: int


class Entitlements:
    """Atomic allowance operations against the entitlements table."""

    def __init__(self, *, session_factory: Callable[[], AsyncSession], settings: Settings) -> None:
        self._session_factory = session_factory
        self._settings = settings

    async def consume_attempt(
        self, user_id: uuid.UUID, *, plan: Plan, count: int = 1
    ) -> QuotaConsumption:
        """Spend daily attempt allowance, or refuse.

        Raises:
            InsufficientQuota: the plan's daily cap would be exceeded.
            EntitlementsMissingError: the user has no row.
        """
        return await self._consume_counter(
            user_id,
            column=Entitlement.daily_attempts_used,
            limit=self._settings.daily_attempt_limit(plan),
            count=count,
            quota="attempts",
        )

    async def release_attempt(self, user_id: uuid.UUID, *, count: int = 1) -> QuotaConsumption:
        """Give back an attempt that was spent on a failed assessment.

        A compensating action, not a general-purpose grant, and the distinction
        is the reason this is its own method rather than ``consume_attempt(-1)``:
        a negative consume would flow through the same guarded UPDATE as a real
        deduction, and a bug that passed a negative count anywhere would become
        a silent refund. ``_require_positive`` exists to stop exactly that, so
        the legitimate case gets its own door.

        The only caller is the attempts endpoint, on the path where the scorer
        returned ok=False. ARCHITECTURE section 5: a provider failure must not
        cost the learner an attempt (docs/DECISIONS.md D-034).

        Floored at zero, so a double release — a retry of the compensation, or
        a reset that landed in between — cannot manufacture allowance out of a
        counter that is already empty.

        Raises:
            EntitlementsMissingError: the user has no row.
        """
        _require_positive(count, "count")

        column = Entitlement.daily_attempts_used
        statement = (
            sa.update(Entitlement)
            .where(Entitlement.user_id == user_id)
            .values({column: sa.func.greatest(column - count, 0)})
            .returning(column)
        )

        async with self._session_factory() as session:
            result = await session.execute(statement)
            used: int | None = result.scalar_one_or_none()
            if used is None:
                raise EntitlementsMissingError(_missing_message(user_id))
            await session.commit()

        log.info(
            "entitlements.released",
            user_id=str(user_id),
            quota="attempts",
            released=count,
            used=used,
        )
        return QuotaConsumption(consumed=-count, remaining=None)

    async def consume_task(
        self, user_id: uuid.UUID, *, plan: Plan, count: int = 1
    ) -> QuotaConsumption:
        """Spend daily task allowance, or refuse."""
        return await self._consume_counter(
            user_id,
            column=Entitlement.daily_tasks_used,
            limit=self._settings.daily_task_limit(plan),
            count=count,
            quota="tasks",
        )

    async def consume_realtime_seconds(
        self, user_id: uuid.UUID, *, seconds: int
    ) -> QuotaConsumption:
        """Spend realtime balance, or refuse.

        Seconds rather than minutes throughout: minutes are a display unit
        (CODING_STANDARDS section 3), and rounding a partial minute up at the
        storage layer would overcharge every session.

        Raises:
            InsufficientQuota: the balance is smaller than the request.
            EntitlementsMissingError: the user has no row.
        """
        _require_positive(seconds, "seconds")

        column = Entitlement.realtime_seconds_remaining
        statement = (
            sa.update(Entitlement)
            .where(Entitlement.user_id == user_id, column >= seconds)
            .values(realtime_seconds_remaining=column - seconds)
            .returning(column)
        )

        remaining = await self._run(statement, user_id=user_id, quota="realtime_seconds")
        log.info(
            "entitlements.consumed",
            user_id=str(user_id),
            quota="realtime_seconds",
            consumed=seconds,
            remaining=remaining,
        )
        return QuotaConsumption(consumed=seconds, remaining=remaining)

    async def grant_realtime_seconds(self, user_id: uuid.UUID, *, seconds: int) -> QuotaConsumption:
        """Add to the realtime balance — a subscription period or a top-up.

        Additive rather than assigning a total, so a top-up bought while seconds
        remain does not silently delete them.
        """
        _require_positive(seconds, "seconds")

        column = Entitlement.realtime_seconds_remaining
        statement = (
            sa.update(Entitlement)
            .where(Entitlement.user_id == user_id)
            .values(realtime_seconds_remaining=column + seconds)
            .returning(column)
        )

        async with self._session_factory() as session:
            result = await session.execute(statement)
            row: int | None = result.scalar_one_or_none()
            if row is None:
                raise EntitlementsMissingError(_missing_message(user_id))
            await session.commit()

        log.info(
            "entitlements.granted",
            user_id=str(user_id),
            quota="realtime_seconds",
            granted=seconds,
            remaining=row,
        )
        return QuotaConsumption(consumed=-seconds, remaining=row)

    async def snapshot(self, user_id: uuid.UUID) -> QuotaSnapshot:
        """Read the current allowance. Display only — see QuotaSnapshot."""
        async with self._session_factory() as session:
            row = await session.get(Entitlement, user_id)
            if row is None:
                raise EntitlementsMissingError(_missing_message(user_id))
            return QuotaSnapshot(
                daily_attempts_used=row.daily_attempts_used,
                daily_tasks_used=row.daily_tasks_used,
                realtime_seconds_remaining=row.realtime_seconds_remaining,
            )

    async def _consume_counter(
        self,
        user_id: uuid.UUID,
        *,
        column: sa.orm.InstrumentedAttribute[int],
        limit: int,
        count: int,
        quota: str,
    ) -> QuotaConsumption:
        _require_positive(count, "count")

        conditions = [Entitlement.user_id == user_id]
        if limit != UNLIMITED:
            # The ceiling test and the increment in one statement. Splitting
            # them is the race this module exists to prevent.
            conditions.append(column + count <= limit)

        statement = (
            sa.update(Entitlement)
            .where(*conditions)
            .values({column: column + count})
            .returning(column)
        )

        used = await self._run(statement, user_id=user_id, quota=quota)
        remaining = None if limit == UNLIMITED else limit - used
        log.info(
            "entitlements.consumed",
            user_id=str(user_id),
            quota=quota,
            consumed=count,
            remaining=remaining,
        )
        return QuotaConsumption(consumed=count, remaining=remaining)

    async def _run(self, statement: sa.Update, *, user_id: uuid.UUID, quota: str) -> int:
        """Execute a guarded update, turning no-rows into the right error.

        A row count of zero has two causes that the statement cannot tell apart:
        the guard refused, or there is no row at all. Reporting the wrong one
        would send someone hunting a quota bug when the user was never
        provisioned, so the distinction costs one extra query on the failure
        path only.
        """
        async with self._session_factory() as session:
            result = await session.execute(statement)
            row: int | None = result.scalar_one_or_none()
            if row is None:
                await session.rollback()
                exists = await session.get(Entitlement, user_id)
                if exists is None:
                    raise EntitlementsMissingError(_missing_message(user_id))
                log.warning("entitlements.refused", user_id=str(user_id), quota=quota)
                raise InsufficientQuota(
                    f"{quota} allowance exhausted", quota=quota, user_id=str(user_id)
                )
            await session.commit()
            return row


def _require_positive(value: int, name: str) -> None:
    """A zero or negative consume would be a silent refund through the same path."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be int, got {type(value).__name__}")
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")


def _missing_message(user_id: uuid.UUID) -> str:
    return (
        f"user {user_id} has no entitlements row; it is created when the account "
        "is provisioned, not on first use"
    )
