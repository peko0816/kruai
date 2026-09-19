"""GET /api/v1/admin/costs — where the money went.

CLAUDE.md section 8: every external call leaves a cost_ledger row, and a call
with no row counts as not having happened. This is the endpoint that makes those
rows worth writing — spend nobody can see is spend nobody controls, and PRD 11.2
sets a hard per-user monthly ceiling that has to be checked against something.

Three groupings, because three questions get asked: which provider is expensive,
which learner is expensive, and is it getting worse. One ``group_by`` parameter
rather than three endpoints, since the row shape is identical.

**Access is closed by default.** ADMIN_TELEGRAM_IDS ships empty, so until an
operator puts their id in it this endpoint answers 403 to everyone, including
them. An operations endpoint that is open until somebody remembers to close it
is an open endpoint.

Costs are estimates (``cost_usd_cents_est``), written by whoever made the call
from what the provider said it charged. They are not an invoice, and the gap
between them and the real bill is itself worth watching.
"""

from __future__ import annotations

import datetime
import uuid
from enum import StrEnum
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUserDep, SessionDep, SettingsDep
from app.core.config import Settings
from app.core.errors import InvalidQueryWindow, PermissionDenied
from app.core.logging import get_logger
from app.models.commerce import CostLedger
from app.models.users import User

log = get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["ops"])

#: How far back the dashboard looks when nobody says. A month covers the period
#: PRD 11.2's ceilings are stated over.
DEFAULT_WINDOW_DAYS = 30


class CostGrouping(StrEnum):
    """The three questions this endpoint answers."""

    DAY = "day"
    PROVIDER = "provider"
    USER = "user"


class CostRowOut(BaseModel):
    """One aggregated line."""

    #: The day (ISO date), the provider name, or the user id — whichever the
    #: grouping was. Null in the user grouping for calls with no user: content
    #: production is billed to the pipeline, not to a learner (PRD 11.3).
    key: str | None
    cost_usd_cents: int
    #: Units consumed, summed within the group. Mixed units are summed
    #: separately by ``unit`` so seconds and tokens never land in one number.
    unit: str
    quantity: float
    calls: int


class CostReportOut(BaseModel):
    group_by: CostGrouping
    since: datetime.date
    until: datetime.date
    total_usd_cents: int
    #: Ledger rows in the window, before grouping. A total of zero with a
    #: non-zero count means the calls were free, not that nothing happened.
    total_entries: int
    rows: list[CostRowOut]


@router.get("/costs")
async def get_costs(
    session: SessionDep,
    settings: SettingsDep,
    user_id: CurrentUserDep,
    group_by: Annotated[CostGrouping, Query()] = CostGrouping.DAY,
    since: Annotated[datetime.date | None, Query(description="Inclusive, UTC")] = None,
    until: Annotated[datetime.date | None, Query(description="Inclusive, UTC")] = None,
) -> CostReportOut:
    """Aggregate cost_ledger over a window.

    Raises:
        PermissionDenied: the caller is not in ADMIN_TELEGRAM_IDS.
        InvalidQueryWindow: the window ends before it starts.
    """
    await _require_operator(session, user_id=user_id, settings=settings)

    today = datetime.datetime.now(datetime.UTC).date()
    window_until = until or today
    window_since = since or window_until - datetime.timedelta(days=DEFAULT_WINDOW_DAYS)
    if window_since > window_until:
        raise InvalidQueryWindow(since=window_since.isoformat(), until=window_until.isoformat())

    rows = await _aggregate(session, group_by=group_by, since=window_since, until=window_until)

    log.info(
        "admin.costs_read",
        operator_id=str(user_id),
        group_by=group_by.value,
        since=window_since.isoformat(),
        until=window_until.isoformat(),
        rows=len(rows),
        total_usd_cents=sum(row.cost_usd_cents for row in rows),
    )

    return CostReportOut(
        group_by=group_by,
        since=window_since,
        until=window_until,
        total_usd_cents=sum(row.cost_usd_cents for row in rows),
        total_entries=sum(row.calls for row in rows),
        rows=rows,
    )


async def _require_operator(
    session: AsyncSession, *, user_id: uuid.UUID, settings: Settings
) -> None:
    """Refuse anyone whose Telegram id is not on the list.

    The check reads the caller's telegram_id rather than trusting a claim in
    the token: the token carries identity and nothing else (D-025), precisely
    so that a privilege cannot outlive the configuration that granted it.
    """
    if not settings.admin_telegram_ids:
        log.warning("admin.no_operators_configured", user_id=str(user_id))
        raise PermissionDenied(reason="no_operators_configured")

    telegram_id = (
        await session.execute(sa.select(User.telegram_id).where(User.id == user_id))
    ).scalar_one_or_none()
    if telegram_id not in settings.admin_telegram_ids:
        log.warning("admin.refused", user_id=str(user_id))
        raise PermissionDenied(reason="not_an_operator")


async def _aggregate(
    session: AsyncSession,
    *,
    group_by: CostGrouping,
    since: datetime.date,
    until: datetime.date,
) -> list[CostRowOut]:
    """Sum the ledger over the window, grouped as asked.

    ``unit`` is always part of the grouping even though it is not the axis
    anybody asked for. Without it a provider billing seconds and another
    billing tokens would have their quantities added together into a number
    that means nothing — the cost column would still be right, and the one next
    to it would be quietly nonsense.

    The window is half-open on the end in SQL and inclusive in the response:
    ``until`` names a day, and a row at 23:30 on that day belongs to it.
    """
    # Any, reluctantly: the three values are a SQL function and two mapped
    # columns of different types, and ColumnElement is invariant in its
    # parameter, so no single annotation covers them. The dict is private and
    # its values go straight into a query — no business data passes through it
    # (CODING_STANDARDS section 2).
    axes: dict[CostGrouping, Any] = {
        CostGrouping.DAY: sa.func.date_trunc("day", CostLedger.occurred_at),
        CostGrouping.PROVIDER: CostLedger.provider,
        CostGrouping.USER: CostLedger.user_id,
    }
    axis = axes[group_by]

    statement = (
        sa.select(
            axis.label("key"),
            CostLedger.unit,
            sa.func.sum(CostLedger.cost_usd_cents_est).label("cost"),
            sa.func.sum(CostLedger.quantity).label("quantity"),
            sa.func.count().label("calls"),
        )
        .where(
            CostLedger.occurred_at >= _start_of(since),
            CostLedger.occurred_at < _start_of(until + datetime.timedelta(days=1)),
        )
        .group_by(axis, CostLedger.unit)
        # Most expensive first: the dashboard is read to find the problem.
        .order_by(sa.func.sum(CostLedger.cost_usd_cents_est).desc(), axis)
    )

    return [
        CostRowOut(
            key=_render_key(row.key),
            unit=row.unit,
            cost_usd_cents=int(row.cost),
            quantity=float(row.quantity),
            calls=int(row.calls),
        )
        for row in (await session.execute(statement)).all()
    ]


def _start_of(day: datetime.date) -> datetime.datetime:
    """Midnight UTC. The ledger stores instants; the dashboard asks about days.

    UTC rather than any learner's local day: this is an operations view of
    spending, and one bill covers people in several timezones.
    """
    return datetime.datetime.combine(day, datetime.time.min, tzinfo=datetime.UTC)


def _render_key(key: object) -> str | None:
    if key is None:
        return None
    if isinstance(key, datetime.datetime):
        return key.date().isoformat()
    return str(key)


__all__ = ["router"]
