"""Which plan a learner is on right now.

Plan is not a column on ``users``. It is whatever their subscriptions say, and
they can hold more than one row — someone who upgrades mid-period has a basic
row and a pro row alive at the same time. The answer is the best of them.

**``status`` is the answer; the dates are not re-read.** A row can say
``status='active'`` while ``period_end`` is yesterday, because the renewal job
had not run yet, and the tempting fix is to compare against the clock here.
That would put the subscription state machine in two places: ARCHITECTURE 3.4
gives D8b one job — move rows between active, grace, expired and cancelled — and
a read path that second-guesses it means a learner's plan depends on which code
path asked. Same reasoning as docs/DECISIONS.md D-023: once the row exists, the
row decides. Keeping status honest is the scheduled task's responsibility, and
its tests are where that belongs.

The rule itself is a pure function over plan names, so it is unit-tested without
a database; only the lookup needs a session.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Final, cast

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Plan, plan_rank
from app.models.commerce import Subscription

#: The statuses that grant their plan. Grace is included deliberately: it is the
#: window after period_end in which benefits continue (ARCHITECTURE 3.4), and a
#: learner whose payment is a day late should not lose the lesson they are in.
ENTITLING_STATUSES: Final[frozenset[str]] = frozenset({"active", "grace"})

#: What everybody has without paying. PRD 4.3.
FREE_PLAN: Final[Plan] = "free"


def best_plan(plans: Iterable[str]) -> Plan:
    """The highest-ranked plan among live subscriptions, or free if there are none.

    Raises:
        ValueError: a plan name the ladder does not know. Better than silently
            ranking it lowest, which would downgrade a paying learner without
            anything failing.
    """
    ranked = sorted(plans, key=plan_rank)
    return cast(Plan, ranked[-1]) if ranked else FREE_PLAN


async def current_plan(session: AsyncSession, user_id: uuid.UUID) -> Plan:
    """This learner's plan, read from their subscriptions."""
    statement = sa.select(Subscription.plan).where(
        Subscription.user_id == user_id,
        Subscription.status.in_(ENTITLING_STATUSES),
    )
    rows = (await session.execute(statement)).scalars().all()
    return best_plan(rows)
