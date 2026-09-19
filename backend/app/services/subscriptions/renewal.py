"""The subscription state machine (ARCHITECTURE 3.4).

    active ──auto, charged────────────► active, one period later
    active ──auto, declined───────────► active, retried per SUBSCRIPTION_AUTO_CHARGE_RETRY_DAYS
    active ──auto, out of retries─────► grace
    active ──manual, period ended─────► grace
    grace  ──grace window ended───────► expired  (and the plan falls back to free)

**Neither path is assumed.** Which one a subscription is on was decided when it
was opened, by what the acquirer could actually do (D-056), and this module
reads ``renewal_mode`` rather than guessing. A channel that cannot charge again
is not a broken channel — most Cambodian wallets cannot, which is the whole
reason there are two paths.

One thing here is derived rather than stored: what the next period costs.
``subscriptions`` has no currency and no billing period, so both are read back
from the payment that opened the period — every payment row carries them under
its own namespace (D-053). A learner who paid in riel is charged in riel. The
cleaner home for this is two columns on ``subscriptions``; they are not added
here because DATA_MODEL.sql is the authority and D8b does not need them
(docs/DECISIONS.md D-060).
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Plan, Settings
from app.core.logging import get_logger
from app.models.commerce import Payment, Subscription
from app.models.users import User
from app.services.entitlements.pricing import BillingPeriod, period_end, price_for
from app.services.payments.base import Money, PaymentStatus
from app.services.subscriptions.settle import ORDER_DETAILS_FIELD

log = get_logger(__name__)

#: Statuses this machine moves between. 'cancelled' is a learner's own decision
#: and no job produces it.
ACTIVE: Final = "active"
GRACE: Final = "grace"
EXPIRED: Final = "expired"

#: What a period defaults to when the opening payment cannot be read back.
_FALLBACK_PERIOD: Final[BillingPeriod] = "monthly"


@dataclass(frozen=True)
class RenewalTerms:
    """What the next period of a subscription costs, and how long it runs."""

    money: Money
    period: BillingPeriod


@dataclass(frozen=True)
class DueSubscription:
    """One subscription a job has decided to act on."""

    id: uuid.UUID
    user_id: uuid.UUID
    plan: Plan
    period_start: datetime.datetime
    period_end: datetime.datetime
    mandate_ref: str | None
    #: The acquirer that took the original payment. A renewal goes back to the
    #: same one: the mandate is theirs and means nothing anywhere else.
    payment_provider: str | None
    telegram_id: int
    #: The learner's interface language, for anything the job sends them.
    locale: str


def retry_days(settings: Settings) -> tuple[int, ...]:
    """Days to wait before each retry, in order. Empty means no retries."""
    return tuple(settings.subscription_auto_charge_retry_days)


def next_attempt_at(
    now: datetime.datetime, *, failures_so_far: int, settings: Settings
) -> datetime.datetime | None:
    """When to try again after a declined charge, or None when out of tries.

    ``failures_so_far`` counts the attempts that have already been declined,
    including the one that just was. With a schedule of (1, 3) a learner gets
    three attempts in total: the one on the due date, one a day later, and one
    three days after that.
    """
    schedule = retry_days(settings)
    if failures_so_far > len(schedule) or failures_so_far < 1:
        return None
    return now + datetime.timedelta(days=schedule[failures_so_far - 1])


async def terms_for(
    session: AsyncSession, *, subscription: DueSubscription, settings: Settings
) -> RenewalTerms:
    """What to charge for the next period, in the currency they last paid in.

    Raises:
        UnpricedError: the currency they paid in is no longer priced. Better
            than charging them a different amount than they agreed to.
    """
    currency, period = await _last_terms(session, subscription=subscription)
    return RenewalTerms(
        money=price_for(subscription.plan, period, currency, settings=settings),
        period=period,
    )


async def due_for_charge(session: AsyncSession, *, now: datetime.datetime) -> list[DueSubscription]:
    """Auto subscriptions whose charge date has arrived."""
    return await _select_due(
        session,
        sa.and_(
            Subscription.status == ACTIVE,
            Subscription.renewal_mode == "auto",
            Subscription.next_charge_at.is_not(None),
            Subscription.next_charge_at <= now,
        ),
    )


async def due_for_reminder(
    session: AsyncSession, *, now: datetime.datetime, settings: Settings
) -> list[DueSubscription]:
    """Manual subscriptions close enough to expiry to warn about, once each.

    ``reminder_sent_at`` is the duplicate guard the DDL put there: a learner
    who gets the same message every hour until they pay has been nagged, not
    reminded.
    """
    horizon = now + datetime.timedelta(days=settings.subscription_reminder_days_before)
    return await _select_due(
        session,
        sa.and_(
            Subscription.status == ACTIVE,
            Subscription.renewal_mode == "manual",
            Subscription.period_end <= horizon,
            Subscription.reminder_sent_at.is_(None),
        ),
    )


async def due_for_lapse(session: AsyncSession, *, now: datetime.datetime) -> list[DueSubscription]:
    """Subscriptions whose paid period is over and which nothing will renew.

    Auto subscriptions with a charge still scheduled are left alone: their own
    job owns them, and cutting off a learner because a charge is running late
    would be a worse error than letting it run a day over.
    """
    return await _select_due(
        session,
        sa.and_(
            Subscription.status == ACTIVE,
            Subscription.period_end <= now,
            sa.or_(
                Subscription.renewal_mode == "manual",
                Subscription.next_charge_at.is_(None),
            ),
        ),
    )


async def due_for_expiry(session: AsyncSession, *, now: datetime.datetime) -> list[DueSubscription]:
    """Subscriptions whose grace window has run out."""
    return await _select_due(
        session,
        sa.and_(
            Subscription.status == GRACE,
            Subscription.grace_until.is_not(None),
            Subscription.grace_until <= now,
        ),
    )


async def extend(
    session: AsyncSession,
    *,
    subscription: DueSubscription,
    period: BillingPeriod,
    now: datetime.datetime,
) -> datetime.datetime:
    """Move a charged subscription into its next period.

    The new period runs from the old one's end rather than from now, so a
    charge that lands a day late does not shorten what the learner bought or
    walk their billing date through the calendar.
    """
    starts = subscription.period_end
    ends = period_end(starts, period)
    await session.execute(
        _update(subscription).values(
            period_start=starts,
            period_end=ends,
            next_charge_at=ends,
            status=ACTIVE,
            grace_until=None,
            reminder_sent_at=None,
        )
    )
    return ends


async def schedule_retry(
    session: AsyncSession, *, subscription: DueSubscription, at: datetime.datetime
) -> None:
    await session.execute(_update(subscription).values(next_charge_at=at))


async def enter_grace(
    session: AsyncSession,
    *,
    subscription: DueSubscription,
    settings: Settings,
) -> datetime.datetime:
    """Benefits continue for a few days after the money stops.

    Measured from the end of the paid period rather than from now: the courtesy
    is a fixed number of days past what they paid for, not past whenever a job
    happened to notice.
    """
    until = subscription.period_end + datetime.timedelta(days=settings.subscription_grace_days)
    await session.execute(
        _update(subscription).values(status=GRACE, grace_until=until, next_charge_at=None)
    )
    return until


async def expire(session: AsyncSession, *, subscription: DueSubscription) -> None:
    """Stop the benefits. current_plan() reads status, so this is the downgrade."""
    await session.execute(_update(subscription).values(status=EXPIRED, next_charge_at=None))


async def mark_reminded(
    session: AsyncSession, *, subscription: DueSubscription, now: datetime.datetime
) -> None:
    """Record that the reminder went out. Only ever called after it did."""
    await session.execute(_update(subscription).values(reminder_sent_at=now))


async def record_charge(
    session: AsyncSession,
    *,
    subscription: DueSubscription,
    order_id: str,
    provider_name: str,
    terms: RenewalTerms,
    status: PaymentStatus,
    provider_ref: str | None,
    now: datetime.datetime,
) -> None:
    """Write the payments row for one recurring charge, succeeded or not.

    Failures are recorded as well as successes, and not only for the audit: the
    number of failed recurring charges in this period is how the retry schedule
    knows which attempt it is on, without a column that would have to be reset.
    """
    await session.execute(
        sa.insert(Payment).values(
            user_id=subscription.user_id,
            order_id=order_id,
            provider=provider_name,
            provider_ref=provider_ref,
            amount_minor=terms.money.amount_minor,
            currency=terms.money.currency,
            currency_minor_units=terms.money.currency_minor_units,
            is_recurring_charge=True,
            status=status.value,
            raw_payload={
                ORDER_DETAILS_FIELD: {
                    "plan": subscription.plan,
                    "period": terms.period,
                }
            },
            created_at=now,
            updated_at=now,
        )
    )


async def failed_charges_this_period(
    session: AsyncSession, *, subscription: DueSubscription
) -> int:
    """How many recurring charges have been declined since this period began."""
    counted = await session.execute(
        sa.select(sa.func.count())
        .select_from(Payment)
        .where(
            Payment.user_id == subscription.user_id,
            Payment.is_recurring_charge.is_(True),
            Payment.status == PaymentStatus.FAILED.value,
            Payment.created_at >= subscription.period_start,
        )
    )
    return int(counted.scalar_one())


# ------------------------------------------------------------------ internals


def _update(subscription: DueSubscription) -> Any:
    return sa.update(Subscription).where(Subscription.id == subscription.id)


async def _select_due(session: AsyncSession, where: Any) -> list[DueSubscription]:
    """Subscriptions matching a condition, joined to the learner behind them.

    The join is an inner one, so a subscription whose user was deleted drops
    out rather than becoming a job that can never finish.
    """
    statement = (
        sa.select(
            Subscription.id,
            Subscription.user_id,
            Subscription.plan,
            Subscription.period_start,
            Subscription.period_end,
            Subscription.mandate_ref,
            Subscription.payment_provider,
            User.telegram_id,
            User.locale,
        )
        .join(User, User.id == Subscription.user_id)
        .where(where, User.deleted_at.is_(None))
        .order_by(Subscription.period_end)
    )
    return [
        DueSubscription(
            id=row.id,
            user_id=row.user_id,
            plan=row.plan,
            period_start=row.period_start,
            period_end=row.period_end,
            mandate_ref=row.mandate_ref,
            payment_provider=row.payment_provider,
            telegram_id=row.telegram_id,
            locale=row.locale,
        )
        for row in (await session.execute(statement)).all()
    ]


async def _last_terms(
    session: AsyncSession, *, subscription: DueSubscription
) -> tuple[str, BillingPeriod]:
    """The currency and billing period of the payment that opened this period."""
    row = (
        await session.execute(
            sa.select(Payment.currency, Payment.raw_payload)
            .where(
                Payment.user_id == subscription.user_id,
                Payment.status == PaymentStatus.SUCCEEDED.value,
            )
            .order_by(Payment.created_at.desc())
            .limit(1)
        )
    ).first()

    if row is None:
        log.error(
            "subscriptions.terms_unreadable",
            subscription_id=str(subscription.id),
            user_id=str(subscription.user_id),
        )
        return "USD", _FALLBACK_PERIOD

    currency, payload = row
    period = ((payload or {}).get(ORDER_DETAILS_FIELD) or {}).get("period")
    if period not in ("monthly", "yearly"):
        log.error(
            "subscriptions.period_unreadable",
            subscription_id=str(subscription.id),
            period=period,
        )
        return currency, _FALLBACK_PERIOD
    return currency, period


__all__ = [
    "ACTIVE",
    "EXPIRED",
    "GRACE",
    "DueSubscription",
    "RenewalTerms",
    "due_for_charge",
    "due_for_expiry",
    "due_for_lapse",
    "due_for_reminder",
    "enter_grace",
    "expire",
    "extend",
    "failed_charges_this_period",
    "mark_reminded",
    "next_attempt_at",
    "record_charge",
    "retry_days",
    "schedule_retry",
    "terms_for",
]
