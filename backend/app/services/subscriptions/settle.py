"""Turning a paid order into a subscription, from wherever the news arrives.

Two things tell us an order was paid: the acquirer's callback, and — when that
callback never arrives — asking them. Both have to end in exactly the same
writes, so the writing lives here rather than inside the webhook handler that
happened to need it first.

**Exactly once, whoever asks.** The guard is a conditional UPDATE on the
payment row: whoever moves it to succeeded is the one that grants the plan, and
everybody else finds nothing to move. That covers an acquirer retrying, a
reconciliation job racing the callback it was compensating for, and two workers
running at once.

The provider is passed in rather than looked up. This is the domain layer, so
it may name ``payments/base.py`` and nothing else under the adapter packages
(ARCHITECTURE section 1) — and the caller knowing which acquirer it is talking
to is the arrangement that keeps the registry out of here.
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
from app.services.entitlements.pricing import BillingPeriod, period_end
from app.services.payments.base import PaymentProvider, PaymentStatus

log = get_logger(__name__)

#: Where our own order details live inside payments.raw_payload, namespaced so
#: they cannot collide with whatever the acquirer sends back (D-053).
ORDER_DETAILS_FIELD: Final = "kruai_order"
CALLBACK_FIELD: Final = "provider_callback"

#: What a settled order defaults to if its details are unreadable. Reached only
#: if raw_payload was written by something other than the checkout endpoint;
#: the cheaper plan is the safer guess, and the log line says it happened.
_FALLBACK_PLAN: Final[Plan] = "basic"
_FALLBACK_PERIOD: Final[BillingPeriod] = "monthly"


@dataclass(frozen=True)
class Settlement:
    """What settling an order did."""

    #: False when there was nothing to settle: no such order, or it was already
    #: settled by a callback, a retry, or another worker.
    granted: bool
    user_id: uuid.UUID | None = None
    plan: Plan | None = None
    period: BillingPeriod | None = None
    renewal_mode: str | None = None


async def settle_order(
    session: AsyncSession,
    *,
    provider: PaymentProvider,
    order_id: str,
    provider_ref: str | None,
    mandate_ref: str | None,
    raw: dict[str, Any],
    settings: Settings,
    now: datetime.datetime,
) -> Settlement:
    """Mark an order paid and grant what it bought, at most once.

    Does not commit: the caller owns the transaction, because a webhook and a
    reconciliation job have different ideas about what else belongs in it.
    """
    claimed = await session.execute(
        sa.update(Payment)
        .where(Payment.order_id == order_id, Payment.status != PaymentStatus.SUCCEEDED.value)
        .values(status=PaymentStatus.SUCCEEDED.value, provider_ref=provider_ref, updated_at=now)
        .returning(Payment.user_id, Payment.raw_payload)
    )
    row = claimed.one_or_none()
    if row is None:
        return Settlement(granted=False)

    user_id, payload = row
    # The acquirer's own payload is kept beside our order details rather than
    # over them: reconciliation later wants both, and neither is the other's.
    await session.execute(
        sa.update(Payment)
        .where(Payment.order_id == order_id)
        .values(raw_payload={**(payload or {}), CALLBACK_FIELD: raw})
    )

    plan, period = _ordered(payload, order_id=order_id)
    renewal_mode = await activate(
        session,
        provider=provider,
        user_id=user_id,
        plan=plan,
        period=period,
        mandate_ref=mandate_ref,
        now=now,
    )
    return Settlement(
        granted=True, user_id=user_id, plan=plan, period=period, renewal_mode=renewal_mode
    )


async def activate(
    session: AsyncSession,
    *,
    provider: PaymentProvider,
    user_id: uuid.UUID,
    plan: Plan,
    period: BillingPeriod,
    mandate_ref: str | None,
    now: datetime.datetime,
) -> str:
    """Write the subscription row for a paid period; return its renewal mode.

    ``renewal_mode`` is decided by what the channel can actually do, never
    assumed (ARCHITECTURE 3.4). Auto also needs a mandate to charge against —
    the database enforces that pairing — so a provider that supports recurring
    but returned no mandate lands in manual, which is the recoverable side.
    """
    ends = period_end(now, period)
    automatic = provider.supports_recurring and bool(mandate_ref)

    await session.execute(
        sa.insert(Subscription).values(
            user_id=user_id,
            plan=plan,
            status="active",
            renewal_mode="auto" if automatic else "manual",
            period_start=now,
            period_end=ends,
            payment_provider=provider.name,
            mandate_ref=mandate_ref if automatic else None,
            # D8b drives both timers; the row has to carry the right one from
            # the moment it exists or the first renewal is the one that is late.
            next_charge_at=ends if automatic else None,
        )
    )
    return "auto" if automatic else "manual"


def _ordered(payload: dict[str, Any] | None, *, order_id: str) -> tuple[Plan, BillingPeriod]:
    """What this order bought, out of the details checkout stored."""
    details = (payload or {}).get(ORDER_DETAILS_FIELD) or {}
    plan = details.get("plan")
    period = details.get("period")
    if plan not in ("basic", "pro") or period not in ("monthly", "yearly"):
        log.error(
            "payments.order_details_unreadable",
            order_id=order_id,
            plan=plan,
            period=period,
        )
        return _FALLBACK_PLAN, _FALLBACK_PERIOD
    return plan, period


__all__ = ["CALLBACK_FIELD", "ORDER_DETAILS_FIELD", "Settlement", "activate", "settle_order"]
