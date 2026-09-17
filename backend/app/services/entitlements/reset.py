"""When a learner's daily allowance comes back.

Resets happen at QUOTA_RESET_HOUR_LOCAL in the learner's own timezone, not at a
single global instant. A Cambodian learner's day should roll over at their
midnight; anything else means the allowance arrives mid-evening for some users.

Two things here are easy to get wrong and neither fails loudly.

Tomorrow is a calendar day, not 24 hours. On a day the clocks move, consecutive
local midnights are 23 or 25 hours apart, so advancing the UTC instant by a day
lands an hour off — and the error compounds at every transition. Note the hazard
is specifically arithmetic on the *converted* value: adding a timedelta to a
zoneinfo-aware datetime moves the wall clock and re-resolves the offset, which
gives the same answer as rebuilding from the date. Converting first and then
adding does not.

Local midnight does not always exist, and sometimes exists twice. Clocks in
Santiago and Beirut jump straight from 23:59 to 01:00 on one day a year, and
Havana's midnight happens twice on another. Python's default fold=0 resolves
both the way a quota reset should — past the gap, and at the first of a repeated
pair — but that is a property worth pinning rather than inheriting quietly
(docs/DECISIONS.md D-019).
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.models.commerce import Entitlement
from app.services.entitlements.quota import EntitlementsMissingError, _missing_message

log = get_logger(__name__)


@dataclass(frozen=True)
class ResetOutcome:
    """Whether the daily counters were cleared, and when they next will be."""

    performed: bool
    next_reset_at: datetime.datetime


def load_timezone(name: str) -> ZoneInfo:
    """Resolve a user_profiles.timezone value.

    Raises:
        ValueError: not a zone the system knows. The column is free text, so a
            typo is possible, and a user whose zone cannot be resolved would
            otherwise never have their allowance reset at all.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown timezone {name!r} in user_profiles.timezone") from exc


def next_reset_at(
    now: datetime.datetime, *, timezone: str, settings: Settings
) -> datetime.datetime:
    """The next instant at which this learner's local clock reads the reset hour.

    Strictly after ``now``: a reset lands exactly on the boundary, and returning
    that same instant would leave the row due again immediately.

    Args:
        now: timezone-aware. UTC in production; any correct offset works.
        timezone: an IANA name, from user_profiles.timezone.

    Raises:
        ValueError: a naive ``now`` or an unresolvable timezone.
    """
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("now must be timezone-aware to be placed on a local clock")

    zone = load_timezone(timezone)
    local = now.astimezone(zone)
    hour = settings.quota_reset_hour_local

    candidate = _local_reset_on(local.date(), hour=hour, zone=zone)
    if candidate <= now:
        # One calendar day later, not one timedelta later: on a transition day
        # those differ by an hour, and the error compounds at every transition.
        candidate = _local_reset_on(local.date() + datetime.timedelta(days=1), hour=hour, zone=zone)
    return candidate.astimezone(datetime.UTC)


def _local_reset_on(day: datetime.date, *, hour: int, zone: ZoneInfo) -> datetime.datetime:
    """The reset instant for one local calendar day.

    fold is left at its default of 0, which is the right answer on both kinds of
    transition day and is asserted in the tests rather than assumed:

      · when local midnight is skipped, fold=0 resolves to the first instant
        after the gap — the moment the clock passes the reset hour;
      · when it happens twice, fold=0 is the earlier of the two, so the
        allowance returns at the first opportunity rather than an hour late.
    """
    return datetime.datetime.combine(day, datetime.time(hour=hour), tzinfo=zone)


class QuotaReset:
    """Clears daily counters when their time has come."""

    def __init__(self, *, session_factory: Callable[[], AsyncSession], settings: Settings) -> None:
        self._session_factory = session_factory
        self._settings = settings

    async def reset_if_due(
        self, user_id: uuid.UUID, *, timezone: str, now: datetime.datetime
    ) -> ResetOutcome:
        """Clear the daily counters if the stored reset time has passed.

        Guarded by ``reset_at <= now`` so it is idempotent: two requests arriving
        together produce one reset, and calling it on every request is safe.

        The realtime balance is deliberately untouched. Those seconds are bought
        by the month and carried, not granted daily — zeroing them here would
        delete something a learner paid for.

        Raises:
            EntitlementsMissingError: the user has no entitlements row.
        """
        upcoming = next_reset_at(now, timezone=timezone, settings=self._settings)

        statement = (
            sa.update(Entitlement)
            .where(Entitlement.user_id == user_id, Entitlement.reset_at <= now)
            .values(daily_attempts_used=0, daily_tasks_used=0, reset_at=upcoming)
            .returning(Entitlement.reset_at)
        )

        async with self._session_factory() as session:
            result = await session.execute(statement)
            performed = result.scalar_one_or_none() is not None
            if not performed:
                await session.rollback()
                stored = await session.get(Entitlement, user_id)
                if stored is None:
                    raise EntitlementsMissingError(_missing_message(user_id))
                # Not yet due, or another request just did it. Either way the
                # stored reset_at is authoritative, not the one computed above.
                return ResetOutcome(performed=False, next_reset_at=stored.reset_at)
            await session.commit()

        log.info(
            "entitlements.reset",
            user_id=str(user_id),
            timezone=timezone,
            next_reset_at=upcoming.isoformat(),
        )
        return ResetOutcome(performed=True, next_reset_at=upcoming)
