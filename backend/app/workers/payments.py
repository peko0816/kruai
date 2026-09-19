"""Chase the payments whose callback never arrived.

An acquirer's webhook is a best-effort delivery over somebody else's network.
When one is lost the learner has paid and has nothing: the order sits at
pending forever, no subscription exists, and **nothing in the system is looking
for it**. ``PaymentProvider.query_status`` exists for exactly this — the
interface calls it 对账兜底, the fallback when a callback goes missing — and
until this module there was no caller.

The job asks the acquirer about orders that have been pending long enough to be
suspicious, and settles the ones they say succeeded through the same code the
webhook uses (services/subscriptions). Racing a callback is safe: whichever
arrives second finds the order already settled and grants nothing.

Orders still pending after PAYMENT_ABANDON_AFTER_HOURS are marked failed. A
checkout somebody opened and walked away from is the common case, and leaving
them pending forever makes "how many payments are stuck" unanswerable.
"""

from __future__ import annotations

import asyncio
import datetime
import uuid
from collections.abc import Callable
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import create_engine, create_session_factory
from app.core.logging import configure_logging, get_logger
from app.models.commerce import Payment
from app.services.cost_ledger import CostLedger
from app.services.payments.base import PaymentStatus
from app.services.payments.registry import get_payment_provider
from app.services.provider_errors import ProviderConfigurationError
from app.services.subscriptions import settle_order

log = get_logger(__name__)


@dataclass(frozen=True)
class ReconcileReport:
    """What one pass did. Every number is worth an alert if it stops being 0."""

    examined: int = 0
    settled: int = 0
    failed: int = 0
    abandoned: int = 0
    unreachable: int = 0


async def reconcile_pending_payments(
    *,
    session_factory: Callable[[], AsyncSession],
    settings: Settings,
    now: datetime.datetime | None = None,
) -> ReconcileReport:
    """Ask the acquirer about every payment that has been pending too long."""
    moment = now or datetime.datetime.now(datetime.UTC)
    ripe = moment - datetime.timedelta(minutes=settings.payment_reconcile_after_minutes)
    dead = moment - datetime.timedelta(hours=settings.payment_abandon_after_hours)

    async with session_factory() as session:
        pending = (
            await session.execute(
                sa.select(Payment.order_id, Payment.provider, Payment.created_at, Payment.user_id)
                .where(
                    Payment.status == PaymentStatus.PENDING.value,
                    Payment.created_at <= ripe,
                )
                .order_by(Payment.created_at)
            )
        ).all()

        report = ReconcileReport(examined=len(pending))
        for order_id, provider_name, created_at, user_id in pending:
            report = await _chase(
                session,
                session_factory=session_factory,
                report=report,
                settings=settings,
                order_id=order_id,
                provider_name=provider_name,
                user_id=user_id,
                abandon=created_at <= dead,
                now=moment,
            )
        await session.commit()

    log.info(
        "payments.reconciled",
        examined=report.examined,
        settled=report.settled,
        failed=report.failed,
        abandoned=report.abandoned,
        unreachable=report.unreachable,
    )
    return report


async def _chase(
    session: AsyncSession,
    *,
    session_factory: Callable[[], AsyncSession],
    report: ReconcileReport,
    settings: Settings,
    order_id: str,
    provider_name: str,
    user_id: uuid.UUID,
    abandon: bool,
    now: datetime.datetime,
) -> ReconcileReport:
    """Ask about one order and act on the answer."""
    try:
        provider = get_payment_provider(provider_name, settings=settings)
    except ProviderConfigurationError:
        # The channel that took this order is no longer enabled. Leaving it
        # pending is the honest answer: nobody can ask, so nobody can settle.
        log.warning("payments.reconcile_unreachable", order_id=order_id, provider=provider_name)
        return _with(report, unreachable=1)

    ledger = CostLedger(session_factory=session_factory, settings=settings)
    async with ledger.external_call(
        provider=provider.name, ref="payment", user_id=user_id
    ) as entry:
        status = await provider.query_status(order_id)
        entry.record(unit="calls", quantity=1, cost_usd_cents=0)

    if status is PaymentStatus.SUCCEEDED:
        settlement = await settle_order(
            session,
            provider=provider,
            order_id=order_id,
            provider_ref=None,
            mandate_ref=None,
            raw={"reconciled": True, "at": now.isoformat()},
            settings=settings,
            now=now,
        )
        if settlement.granted:
            log.info(
                "payments.reconciled_settled",
                order_id=order_id,
                plan=settlement.plan,
                renewal_mode=settlement.renewal_mode,
            )
            return _with(report, settled=1)
        # A callback arrived while we were asking. Nothing to do, and nothing
        # wrong: that is the race this is allowed to lose.
        return report

    if status is PaymentStatus.FAILED:
        await _mark(session, order_id=order_id, status=PaymentStatus.FAILED, now=now)
        return _with(report, failed=1)

    if abandon:
        # Still pending after the window. A checkout opened and walked away
        # from is the common case; leaving it pending forever makes "how many
        # payments are stuck" unanswerable.
        await _mark(session, order_id=order_id, status=PaymentStatus.FAILED, now=now)
        log.info("payments.reconcile_abandoned", order_id=order_id)
        return _with(report, abandoned=1)

    return report


async def _mark(
    session: AsyncSession, *, order_id: str, status: PaymentStatus, now: datetime.datetime
) -> None:
    await session.execute(
        sa.update(Payment)
        .where(Payment.order_id == order_id, Payment.status == PaymentStatus.PENDING.value)
        .values(status=status.value, updated_at=now)
    )


def _with(report: ReconcileReport, **increments: int) -> ReconcileReport:
    return ReconcileReport(
        examined=report.examined,
        settled=report.settled + increments.get("settled", 0),
        failed=report.failed + increments.get("failed", 0),
        abandoned=report.abandoned + increments.get("abandoned", 0),
        unreachable=report.unreachable + increments.get("unreachable", 0),
    )


async def _run() -> ReconcileReport:
    settings = Settings()
    configure_logging(log_level=settings.log_level, json_output=settings.env != "dev")
    engine = create_engine(settings)
    try:
        return await reconcile_pending_payments(
            session_factory=create_session_factory(engine), settings=settings
        )
    finally:
        await engine.dispose()


def main() -> int:
    report = asyncio.run(_run())
    print(  # this is a CLI
        f"examined={report.examined} settled={report.settled} failed={report.failed} "
        f"abandoned={report.abandoned} unreachable={report.unreachable}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
