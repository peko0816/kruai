"""The jobs that keep subscriptions moving (ARCHITECTURE 3.4).

Three of them, and between them they are the only reason a subscription ever
changes state after it is opened:

  · ``charge_due_subscriptions`` — the auto path. Charge, or retry, or give up
    into grace.
  · ``remind_expiring_subscriptions`` — the manual path. One message before the
    period ends, and only one.
  · ``lapse_finished_subscriptions`` — both paths end here: a period that ran
    out drops into grace, and a grace window that ran out expires.

Without them a subscription is bought once and never changes: a paying learner
is charged a single time and served forever, and one who stopped paying keeps
everything. Nothing would be wrong in any log.

**Neither renewal path is assumed anywhere.** The charge job reads
``renewal_mode`` and only ever touches auto subscriptions; the reminder job
only ever touches manual ones. A channel that cannot charge again — which is
most Cambodian wallets — is served by the second, not failed by the first.

Every job takes its dependencies as arguments and returns a report. Scheduling
them is a deployment concern: cron, RQ or a person at a terminal.
"""

from __future__ import annotations

import asyncio
import datetime
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import create_engine, create_session_factory
from app.core.logging import configure_logging, get_logger
from app.services.entitlements.pricing import UnpricedError
from app.services.payments.base import PaymentProvider, PaymentStatus
from app.services.payments.registry import get_payment_provider
from app.services.provider_errors import ProviderConfigurationError
from app.services.subscriptions import renewal
from app.services.subscriptions.renewal import DueSubscription

log = get_logger(__name__)

#: Prefix for the order number of a recurring charge, so the ledger and the
#: acquirer's dashboard both show at a glance which payments were automatic.
RECURRING_ORDER_PREFIX = "kruai_r"

#: What the manual path says. Named ``_KEY`` because that is how
#: bot/i18n_check.py recognises a message key — passing the literal as a
#: keyword argument makes it invisible to the check, which is how this one was
#: found the first time.
RENEWAL_REMINDER_KEY = "bot.renewal_reminder"


class ReminderSender(Protocol):
    """Whatever puts a renewal reminder in front of a learner.

    A protocol rather than a concrete Telegram call so the reminder job can be
    tested without a bot token — and so that a reminder is only ever recorded
    as sent when something actually sent it.
    """

    async def send(self, telegram_id: int, *, message_key: str, locale: str) -> bool:
        """True when the learner received it."""
        ...


@dataclass(frozen=True)
class ChargeReport:
    due: int = 0
    charged: int = 0
    retried: int = 0
    graced: int = 0
    unchargeable: int = 0


@dataclass(frozen=True)
class ReminderReport:
    due: int = 0
    sent: int = 0
    undelivered: int = 0


@dataclass(frozen=True)
class LapseReport:
    graced: int = 0
    expired: int = 0


# ----------------------------------------------------------------- auto path


async def charge_due_subscriptions(
    *,
    session_factory: Callable[[], AsyncSession],
    settings: Settings,
    now: datetime.datetime | None = None,
) -> ChargeReport:
    """Charge every auto subscription whose date has come."""
    moment = now or datetime.datetime.now(datetime.UTC)

    async with session_factory() as session:
        due = await renewal.due_for_charge(session, now=moment)
        report = ChargeReport(due=len(due))
        for subscription in due:
            report = await _charge_one(
                session, subscription=subscription, settings=settings, report=report, now=moment
            )
        await session.commit()

    log.info(
        "subscriptions.charged",
        due=report.due,
        charged=report.charged,
        retried=report.retried,
        graced=report.graced,
        unchargeable=report.unchargeable,
    )
    return report


async def _charge_one(
    session: AsyncSession,
    *,
    subscription: DueSubscription,
    settings: Settings,
    report: ChargeReport,
    now: datetime.datetime,
) -> ChargeReport:
    provider = _provider_for(subscription, settings=settings)
    if provider is None or not provider.supports_recurring or not subscription.mandate_ref:
        # The channel cannot charge again. Letting them run to grace is the
        # recoverable answer: a manual reminder can still save the subscription,
        # and charging blind cannot.
        #
        # The missing-mandate half of that condition is unreachable — the
        # auto_needs_mandate constraint in DATA_MODEL.sql forbids the row — and
        # is kept because the column is nullable and the check costs a
        # comparison. A test asserts the constraint rather than this branch.
        await renewal.enter_grace(session, subscription=subscription, settings=settings)
        log.warning(
            "subscriptions.unchargeable",
            subscription_id=str(subscription.id),
            has_mandate=bool(subscription.mandate_ref),
        )
        return replace(report, unchargeable=report.unchargeable + 1)

    try:
        terms = await renewal.terms_for(session, subscription=subscription, settings=settings)
    except UnpricedError:
        # The currency they pay in is no longer priced. Charging a different
        # amount than they agreed to would be worse than letting it lapse.
        await renewal.enter_grace(session, subscription=subscription, settings=settings)
        log.error("subscriptions.unpriced", subscription_id=str(subscription.id))
        return replace(report, unchargeable=report.unchargeable + 1)

    order_id = f"{RECURRING_ORDER_PREFIX}_{uuid.uuid4().hex}"
    result = await provider.charge_recurring(
        mandate_ref=subscription.mandate_ref,
        order_id=order_id,
        money=terms.money,
        product_name=f"{subscription.plan}-{terms.period}",
    )
    charged = result.ok and result.status is PaymentStatus.SUCCEEDED
    await renewal.record_charge(
        session,
        subscription=subscription,
        order_id=order_id,
        provider_name=provider.name,
        terms=terms,
        status=PaymentStatus.SUCCEEDED if charged else PaymentStatus.FAILED,
        provider_ref=result.provider_ref,
        now=now,
    )

    if charged:
        ends = await renewal.extend(
            session, subscription=subscription, period=terms.period, now=now
        )
        log.info(
            "subscriptions.renewed",
            subscription_id=str(subscription.id),
            plan=subscription.plan,
            period=terms.period,
            period_end=ends.isoformat(),
        )
        return replace(report, charged=report.charged + 1)

    failures = await renewal.failed_charges_this_period(session, subscription=subscription)
    retry_at = renewal.next_attempt_at(now, failures_so_far=failures, settings=settings)
    if retry_at is not None:
        await renewal.schedule_retry(session, subscription=subscription, at=retry_at)
        log.warning(
            "subscriptions.charge_declined",
            subscription_id=str(subscription.id),
            attempt=failures,
            retry_at=retry_at.isoformat(),
            error_code=result.error_code,
        )
        return replace(report, retried=report.retried + 1)

    until = await renewal.enter_grace(session, subscription=subscription, settings=settings)
    log.warning(
        "subscriptions.grace",
        subscription_id=str(subscription.id),
        attempts=failures,
        grace_until=until.isoformat(),
    )
    return replace(report, graced=report.graced + 1)


# --------------------------------------------------------------- manual path


async def remind_expiring_subscriptions(
    *,
    session_factory: Callable[[], AsyncSession],
    settings: Settings,
    sender: ReminderSender,
    now: datetime.datetime | None = None,
) -> ReminderReport:
    """Warn manual subscribers once, before their period runs out.

    ``reminder_sent_at`` is written only when the send succeeded. A reminder
    marked as sent and never delivered is the one failure this job must not
    produce — the learner would simply find their plan gone.
    """
    moment = now or datetime.datetime.now(datetime.UTC)

    async with session_factory() as session:
        due = await renewal.due_for_reminder(session, now=moment, settings=settings)
        report = ReminderReport(due=len(due))
        for subscription in due:
            delivered = await sender.send(
                subscription.telegram_id,
                message_key=RENEWAL_REMINDER_KEY,
                locale=subscription.locale,
            )
            if not delivered:
                log.warning(
                    "subscriptions.reminder_undelivered",
                    subscription_id=str(subscription.id),
                )
                report = replace(report, undelivered=report.undelivered + 1)
                continue
            await renewal.mark_reminded(session, subscription=subscription, now=moment)
            report = replace(report, sent=report.sent + 1)
        await session.commit()

    log.info(
        "subscriptions.reminded",
        due=report.due,
        sent=report.sent,
        undelivered=report.undelivered,
    )
    return report


# ---------------------------------------------------------- the end of both


async def lapse_finished_subscriptions(
    *,
    session_factory: Callable[[], AsyncSession],
    settings: Settings,
    now: datetime.datetime | None = None,
) -> LapseReport:
    """Drop finished periods into grace, and finished grace into expiry.

    Both in one pass and in that order, so a subscription cannot be expired in
    the same run that granted it its grace days.
    """
    moment = now or datetime.datetime.now(datetime.UTC)

    async with session_factory() as session:
        expiring = await renewal.due_for_expiry(session, now=moment)
        for subscription in expiring:
            await renewal.expire(session, subscription=subscription)
            log.info(
                "subscriptions.expired",
                subscription_id=str(subscription.id),
                plan=subscription.plan,
            )

        lapsing = await renewal.due_for_lapse(session, now=moment)
        for subscription in lapsing:
            until = await renewal.enter_grace(session, subscription=subscription, settings=settings)
            log.info(
                "subscriptions.grace",
                subscription_id=str(subscription.id),
                grace_until=until.isoformat(),
            )

        await session.commit()

    report = LapseReport(graced=len(lapsing), expired=len(expiring))
    log.info("subscriptions.lapsed", graced=report.graced, expired=report.expired)
    return report


# ------------------------------------------------------------------ plumbing


def _provider_for(subscription: DueSubscription, *, settings: Settings) -> PaymentProvider | None:
    """The channel that took the original payment, if it is still enabled.

    A renewal goes back to the same acquirer: the mandate belongs to them and
    means nothing anywhere else. None when that channel has been switched off,
    which is a configuration change rather than a declined card — so the
    subscription lapses gently rather than failing loudly.
    """
    if not subscription.payment_provider:
        return None
    try:
        return get_payment_provider(subscription.payment_provider, settings=settings)
    except ProviderConfigurationError:
        return None


async def _run(settings: Settings, sender: ReminderSender) -> None:
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        await charge_due_subscriptions(session_factory=factory, settings=settings)
        await remind_expiring_subscriptions(
            session_factory=factory, settings=settings, sender=sender
        )
        await lapse_finished_subscriptions(session_factory=factory, settings=settings)
    finally:
        await engine.dispose()


def main() -> int:
    from app.workers.reminders import TelegramReminder

    settings = Settings()
    configure_logging(log_level=settings.log_level, json_output=settings.env != "dev")
    asyncio.run(_run(settings, TelegramReminder(settings)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ChargeReport",
    "LapseReport",
    "ReminderReport",
    "ReminderSender",
    "charge_due_subscriptions",
    "lapse_finished_subscriptions",
    "remind_expiring_subscriptions",
]
