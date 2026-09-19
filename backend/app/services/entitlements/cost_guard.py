"""The per-user cost ceiling (PRD 11.3), and the rounding it hinges on.

PRD section 11 states the per-user monthly caps as a hard constraint and 11.3
says what enforcing them means: alert and throttle past
``COST_ALERT_MULTIPLIER`` times the cap. Until now the caps were numbers in a
configuration file that nothing read.

**This is where carry-forward constraint L-1 is discharged.**
``COST_ALERT_MULTIPLIER`` is the only float in the project that touches money.
It is a ratio rather than an amount, so R4 holds — but the comparison it drives
is not exact: 45 x 1.5 is 67.5, and whether 67 counts as over is a decision
somebody has to make rather than something float ordering should decide.

So it is decided here, twice and explicitly:

  1. the ratio is turned into an integer once (1.5 becomes 1500 per thousand),
     and every comparison after that is integer arithmetic;
  2. the threshold rounds **up** — 67.5 becomes 68 — because "more than 1.5
     times the cap" means more than 67.5, and 67 is not more than 67.5.

A learner at exactly 67 keeps practising. A learner at 68 is throttled. Neither
of those follows from the arithmetic; both are choices, and they are the ones a
person can check.
"""

from __future__ import annotations

import datetime
from typing import Final

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Plan, Settings
from app.models.commerce import CostLedger

#: Fixed-point scale for the multiplier. Three digits is far more precision
#: than a guardrail ratio needs and keeps every product inside an int.
_SCALE: Final = 1000


def scaled_multiplier(settings: Settings) -> int:
    """The alert ratio as an integer per thousand. 1.5 becomes 1500.

    Rounded once, here, rather than at each comparison: a ratio that rounded
    differently depending on the call site would make the threshold depend on
    who asked.
    """
    return round(settings.cost_alert_multiplier * _SCALE)


def alert_threshold_usd_cents(plan: Plan, *, settings: Settings) -> int:
    """The spend at which a learner on this plan is throttled.

    Rounded up: the rule is "past 1.5 times the cap", and with a cap of 45 that
    is past 67.5 — which the integers reach at 68, not at 67.
    """
    cap = settings.monthly_cost_cap_usd_cents(plan)
    product = cap * scaled_multiplier(settings)
    # Ceiling division on integers. -(-a // b) avoids float entirely.
    return -(-product // _SCALE)


def is_over_threshold(spend_usd_cents: int, *, plan: Plan, settings: Settings) -> bool:
    """Whether this learner has spent past their plan's alert threshold."""
    return spend_usd_cents >= alert_threshold_usd_cents(plan, settings=settings)


def month_start(now: datetime.datetime) -> datetime.datetime:
    """Midnight UTC on the first of the month ``now`` falls in.

    UTC rather than the learner's own month: the caps in PRD 11.2 are a
    statement about our monthly bill, and one bill covers people in several
    timezones. The daily allowance is the other way round (C5) because that one
    is about the learner's day.
    """
    return datetime.datetime(now.year, now.month, 1, tzinfo=datetime.UTC)


async def monthly_spend_usd_cents(
    session: AsyncSession, *, user_id: object, now: datetime.datetime
) -> int:
    """What this learner has cost us so far this calendar month.

    Reads cost_ledger, which is the point of insisting every external call
    writes one (CLAUDE.md section 8): a call nobody recorded is spend this
    cannot see, and the ceiling would be enforced against a number that is
    quietly too small.
    """
    total = await session.execute(
        sa.select(sa.func.coalesce(sa.func.sum(CostLedger.cost_usd_cents_est), 0)).where(
            CostLedger.user_id == user_id,
            CostLedger.occurred_at >= month_start(now),
        )
    )
    return int(total.scalar_one())


__all__ = [
    "alert_threshold_usd_cents",
    "is_over_threshold",
    "month_start",
    "monthly_spend_usd_cents",
    "scaled_multiplier",
]
