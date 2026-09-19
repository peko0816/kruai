"""GET /api/v1/me/entitlements — what this learner may do today.

Display only. Every number here is read with ``Entitlements.snapshot()``, which
its own docstring warns must never gate anything: by the time a client acts on
it the counter can already have moved, and only the guarded UPDATE in
consume_attempt is authoritative. This endpoint exists so a client can *show* an
allowance, not so it can decide one.

Unlimited is reported as null rather than as 0. Internally 0 is the sentinel for
"no ceiling" (CONFIG_REFERENCE section 4), and handing that to a client is a
paywall that reads "0 attempts left" to every paying subscriber.
"""

from __future__ import annotations

import datetime
import uuid

import sqlalchemy as sa
from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUserDep, SessionDep, SessionFactoryDep, SettingsDep
from app.core.config import Plan
from app.core.logging import get_logger
from app.models.users import UserProfile
from app.services.entitlements import UNLIMITED, Entitlements, QuotaReset, current_plan

log = get_logger(__name__)

router = APIRouter(prefix="/me", tags=["account"])


class AllowanceOut(BaseModel):
    """One daily counter. ``limit`` and ``remaining`` are null when uncapped."""

    used: int
    limit: int | None
    remaining: int | None


class EntitlementsOut(BaseModel):
    plan: Plan
    attempts: AllowanceOut
    tasks: AllowanceOut
    #: A balance rather than a counter: bought by the month and carried, so it
    #: is not touched by the daily reset.
    realtime_seconds_remaining: int
    #: When the two daily counters go back to zero, in UTC. Local midnight for
    #: this learner, which is not the same instant for everybody (C5).
    resets_at: datetime.datetime


@router.get("/entitlements")
async def get_entitlements(
    session: SessionDep,
    session_factory: SessionFactoryDep,
    settings: SettingsDep,
    user_id: CurrentUserDep,
) -> EntitlementsOut:
    """This learner's plan and what is left of today's allowance.

    The daily reset runs here as well as on the attempts path (D-037). Without
    it a learner opening the app just after their local midnight would be shown
    yesterday's exhausted counters, and would believe they were still out of
    attempts until they tried one anyway. ``reset_if_due`` is guarded and
    idempotent, so calling it on a read costs one statement and cannot double.
    """
    now = datetime.datetime.now(datetime.UTC)
    plan = await current_plan(session, user_id)

    timezone = await _timezone(session, user_id)
    quota_reset = QuotaReset(session_factory=session_factory, settings=settings)
    outcome = await quota_reset.reset_if_due(user_id, timezone=timezone, now=now)

    entitlements = Entitlements(session_factory=session_factory, settings=settings)
    snapshot = await entitlements.snapshot(user_id)

    return EntitlementsOut(
        plan=plan,
        attempts=_allowance(snapshot.daily_attempts_used, settings.daily_attempt_limit(plan)),
        tasks=_allowance(snapshot.daily_tasks_used, settings.daily_task_limit(plan)),
        realtime_seconds_remaining=snapshot.realtime_seconds_remaining,
        resets_at=outcome.next_reset_at,
    )


def _allowance(used: int, limit: int) -> AllowanceOut:
    """Turn the internal unlimited sentinel into something a client can render."""
    if limit == UNLIMITED:
        return AllowanceOut(used=used, limit=None, remaining=None)
    # Clamped at zero: a limit lowered mid-day can leave used above it, and a
    # negative "remaining" is not a thing any interface should have to draw.
    return AllowanceOut(used=used, limit=limit, remaining=max(limit - used, 0))


async def _timezone(session: AsyncSession, user_id: uuid.UUID) -> str:
    """The learner's IANA zone, which the reset needs to find their midnight.

    Raises:
        ValueError: no profile row. Sign-in provisions one (D-029), so this is
            a broken invariant rather than a state to paper over — the same
            stance quota.py takes on a missing entitlements row. Substituting
            the DDL default here would restate it in a second place and hide
            the fact that an account is half-provisioned.
    """
    stored = await session.execute(
        sa.select(UserProfile.timezone).where(UserProfile.user_id == user_id)
    )
    timezone: str | None = stored.scalar_one_or_none()
    if timezone is None:
        raise ValueError(
            f"user {user_id} has no user_profiles row; it is created when the "
            "account is provisioned, not on first use"
        )
    return timezone


__all__ = ["router"]
