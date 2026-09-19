"""Taking money, and turning a paid order into a subscription.

    POST /api/v1/payments/checkout
    POST /api/v1/payments/webhook/{provider}

Two halves with very different threat models. The first is a learner asking to
pay and is authenticated like any other endpoint. The second is an unauthenticated
request from the open internet claiming a payment succeeded, and the only thing
standing between it and a free subscription is ``verify_callback``.

**The signature decides.** ARCHITECTURE section 5 is explicit: a callback that
fails verification is refused, never "let through and reconciled later". The
provider contract makes that enforceable by giving back nothing but
``signature_valid=False`` — no order id, no amount — so a caller cannot use the
contents without having checked (services/payments/base.py).

**Activation is idempotent.** Acquirers retry callbacks, and a learner whose
webhook arrived twice must not end up with two subscriptions or two months. The
guard is a conditional UPDATE on the payment row: whoever moves it to succeeded
activates, and the second caller finds nothing to move and does nothing.

Amounts never become floats. The price comes from configuration as an integer
in the smallest unit of its currency, travels as ``Money``, and is stored in
``amount_minor`` beside the currency and its decimal count — which is what lets
៛8000 and $1.99 live in one table without either of them being read as cents.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import CurrentUserDep, SessionDep, SessionFactoryDep, SettingsDep
from app.core.config import Plan, Settings
from app.core.errors import AlreadySubscribed, PaymentRefused
from app.core.logging import get_logger
from app.core.money import format_money
from app.models.commerce import Payment, Subscription
from app.services.cost_ledger import CostLedger
from app.services.entitlements import ENTITLING_STATUSES, Entitlements
from app.services.entitlements.pricing import (
    BILLING_PERIODS,
    PURCHASABLE_PLANS,
    BillingPeriod,
    UnpricedError,
    price_for,
)
from app.services.payments.base import (
    CheckoutRequest,
    Money,
    PaymentProvider,
    PaymentStatus,
    ProductKind,
)
from app.services.payments.registry import get_payment_provider, provider_for_currency
from app.services.provider_errors import ProviderConfigurationError
from app.services.subscriptions import MANDATE_FIELD, ORDER_DETAILS_FIELD, settle_order

log = get_logger(__name__)

router = APIRouter(prefix="/payments", tags=["commerce"])

#: Our own order number, and the idempotency key the acquirer echoes back. No
#: user id in it: an order number travels through a third party's systems and
#: their logs, so it carries nothing about who placed it.
ORDER_ID_PREFIX = "kruai"

#: Our order details live inside payments.raw_payload under a namespace of
#: their own (docs/DECISIONS.md D-053). The name is defined once, in
#: services/subscriptions, because the settling side has to read what this side
#: wrote.


class CheckoutIn(BaseModel):
    plan: Plan
    period: BillingPeriod = "monthly"
    #: Defaults to DEFAULT_CURRENCY. A learner paying in riel and one paying in
    #: dollars are the same flow with different numbers.
    currency: str | None = None
    #: Which acquirer to use. Omitted means whichever settles this currency.
    provider: str | None = None
    return_url: str | None = Field(default=None, max_length=2048)


class CheckoutOut(BaseModel):
    """Where to go to pay. One of the two URLs is always set.

    ``checkout_url`` is a hosted page; ``qr_payload`` is a KHQR string the
    client renders itself. ARCHITECTURE 3.3 lists both because a channel change
    must not reach the client — it already handles either.
    """

    order_id: str
    provider: str
    checkout_url: str | None
    qr_payload: str | None
    amount_minor: int
    currency: str
    currency_minor_units: int
    #: Rendered once, on the server, by core/money.py. The client never does
    #: 10**n arithmetic (CODING_STANDARDS section 3).
    amount_display: str


@router.post("/checkout", status_code=status.HTTP_201_CREATED)
async def create_checkout(
    payload: CheckoutIn,
    session: SessionDep,
    session_factory: SessionFactoryDep,
    settings: SettingsDep,
    user_id: CurrentUserDep,
) -> CheckoutOut:
    """Price a plan, record the order, and ask the acquirer for a way to pay.

    The payment row is written before the acquirer is called. If the call then
    fails the learner has a pending order that nothing will ever complete,
    which is recoverable; the other order would leave money taken against an
    order we have no record of, which is not.

    Raises:
        PaymentRefused: the plan cannot be bought, the currency is not one we
            accept or price, or the acquirer refused the order.
    """
    now = datetime.datetime.now(datetime.UTC)
    currency = (payload.currency or settings.default_currency).upper()
    _check_currency(currency, settings=settings)
    await _refuse_if_already_subscribed(session, user_id=user_id)

    if payload.plan not in PURCHASABLE_PLANS:
        raise PaymentRefused(reason="plan_not_purchasable", plan=payload.plan)
    if payload.period not in BILLING_PERIODS:  # pragma: no cover - pydantic refuses first
        raise PaymentRefused(reason="unknown_period", period=payload.period)

    try:
        money = price_for(payload.plan, payload.period, currency, settings=settings)
    except UnpricedError as exc:
        # A currency we accept but have no price in. The startup self-check
        # refuses this configuration, so reaching it means it changed under us.
        log.error("payments.unpriced", plan=payload.plan, currency=currency, detail=str(exc))
        raise PaymentRefused(reason="unpriced", plan=payload.plan, currency=currency) from exc

    provider = _resolve_provider(payload.provider, currency, settings=settings)
    order_id = f"{ORDER_ID_PREFIX}_{uuid.uuid4().hex}"

    await session.execute(
        sa.insert(Payment).values(
            user_id=user_id,
            order_id=order_id,
            provider=provider.name,
            amount_minor=money.amount_minor,
            currency=money.currency,
            currency_minor_units=money.currency_minor_units,
            is_recurring_charge=False,
            status=PaymentStatus.PENDING.value,
            raw_payload={ORDER_DETAILS_FIELD: {"plan": payload.plan, "period": payload.period}},
            created_at=now,
            updated_at=now,
        )
    )
    await session.commit()

    ledger = CostLedger(session_factory=session_factory, settings=settings)
    async with ledger.external_call(
        provider=provider.name, ref="payment", user_id=user_id
    ) as entry:
        result = await provider.create_checkout(
            CheckoutRequest(
                order_id=order_id,
                money=money,
                product_kind=ProductKind.SUBSCRIPTION,
                product_name=f"{payload.plan}-{payload.period}",
                # Our user id, which is not personal data. The acquirer sees a uuid.
                user_ref=str(user_id),
                setup_recurring=provider.supports_recurring,
                return_url=payload.return_url,
            )
        )
        entry.record(unit="calls", quantity=1, cost_usd_cents=0)

    if not result.ok or not (result.checkout_url or result.qr_payload):
        await _mark_failed(session, order_id=order_id, now=now)
        log.warning(
            "payments.checkout_refused",
            user_id=str(user_id),
            order_id=order_id,
            provider=provider.name,
            error_code=result.error_code,
        )
        raise PaymentRefused(reason=result.error_code or "acquirer_refused")

    await session.execute(
        sa.update(Payment)
        .where(Payment.order_id == order_id)
        .values(
            provider_ref=result.provider_ref,
            updated_at=now,
            # Kept because the callback is not the only way this order can be
            # settled: reconciliation has no mandate to hand over, and without
            # this an order whose callback was lost would quietly renew by
            # reminder instead of by charge (D-066).
            raw_payload={
                ORDER_DETAILS_FIELD: {"plan": payload.plan, "period": payload.period},
                MANDATE_FIELD: result.mandate_ref,
            },
        )
    )
    await session.commit()

    log.info(
        "payments.checkout_created",
        user_id=str(user_id),
        order_id=order_id,
        provider=provider.name,
        plan=payload.plan,
        period=payload.period,
        amount_minor=money.amount_minor,
        currency=money.currency,
    )

    return CheckoutOut(
        order_id=order_id,
        provider=provider.name,
        checkout_url=result.checkout_url,
        qr_payload=result.qr_payload,
        amount_minor=money.amount_minor,
        currency=money.currency,
        currency_minor_units=money.currency_minor_units,
        amount_display=format_money(money),
    )


@router.post("/webhook/{provider_name}")
async def receive_webhook(
    provider_name: str,
    request: Request,
    session: SessionDep,
    session_factory: SessionFactoryDep,
    settings: SettingsDep,
) -> Response:
    """Verify an acquirer's callback and, if it is genuine, grant the plan.

    Unauthenticated by necessity — the acquirer has no token of ours — so the
    signature is the whole of the security boundary. Nothing in the body is
    read until it verifies.

    Answers 200 to anything it has finished with, including a duplicate, so an
    acquirer stops retrying. A bad signature is 400: there is nothing to retry.
    """
    body = await request.body()
    try:
        provider = get_payment_provider(provider_name, settings=settings)
    except ProviderConfigurationError:
        # An unknown or disabled channel. Same answer as a bad signature: this
        # request is not something we will ever accept.
        log.warning("payments.webhook_unknown_provider", provider=provider_name)
        raise PaymentRefused(reason="unknown_provider") from None

    callback = provider.verify_callback(
        headers={key.lower(): value for key, value in request.headers.items()},
        body=body,
        content_type=request.headers.get("content-type", ""),
    )

    if not callback.signature_valid:
        # ARCHITECTURE section 5: refuse, record, and do not grant anything.
        log.warning("payments.webhook_rejected", provider=provider_name, bytes=len(body))
        raise PaymentRefused(reason="signature_invalid")

    if callback.status is not PaymentStatus.SUCCEEDED or not callback.order_id:
        log.info(
            "payments.webhook_ignored",
            provider=provider_name,
            order_id=callback.order_id,
            payment_status=callback.status.value,
        )
        return Response(status_code=status.HTTP_200_OK)

    activated = await _settle(
        session,
        session_factory=session_factory,
        settings=settings,
        provider=provider,
        order_id=callback.order_id,
        provider_ref=callback.provider_ref,
        mandate_ref=callback.mandate_ref,
        raw=callback.raw,
        paid=callback.money,
    )
    log.info(
        "payments.webhook_settled",
        provider=provider_name,
        order_id=callback.order_id,
        activated=activated,
    )
    return Response(status_code=status.HTTP_200_OK)


# ------------------------------------------------------------------ internals


async def _refuse_if_already_subscribed(session: AsyncSession, *, user_id: uuid.UUID) -> None:
    """One live subscription at a time.

    Without this a learner on Basic who buys Pro ends up holding both, and
    **both renew forever** — two charges a month for one account, which nothing
    in the system would ever notice. Upgrading is a real need and a real
    product decision (what happens to the days already paid for), so it is not
    guessed at here: the purchase is refused until somebody decides
    (docs/DECISIONS.md D-065).

    Raises:
        AlreadySubscribed: an active or grace subscription already exists.
    """
    existing = await session.execute(
        sa.select(Subscription.plan).where(
            Subscription.user_id == user_id,
            Subscription.status.in_(ENTITLING_STATUSES),
        )
    )
    held = existing.scalars().first()
    if held is not None:
        log.info("payments.already_subscribed", user_id=str(user_id), plan=held)
        raise AlreadySubscribed(plan=held)


def _check_currency(currency: str, *, settings: Settings) -> None:
    if currency not in settings.supported_currencies:
        raise PaymentRefused(reason="currency_not_supported", currency=currency)


def _resolve_provider(
    requested: str | None, currency: str, *, settings: Settings
) -> PaymentProvider:
    """The acquirer to use: the one asked for, or one that settles this currency.

    Raises:
        PaymentRefused: the name is not enabled, or nothing enabled can settle
            the currency. A misconfiguration, but one a learner meets at
            checkout, so it leaves by the same door as any other refusal.
    """
    try:
        if requested is not None:
            provider = get_payment_provider(requested, settings=settings)
            if currency not in provider.supported_currencies:
                raise PaymentRefused(
                    reason="provider_cannot_settle_currency",
                    provider=requested,
                    currency=currency,
                )
            return provider
        return provider_for_currency(currency, settings=settings)
    except ProviderConfigurationError as exc:
        log.error("payments.provider_unavailable", currency=currency, detail=str(exc))
        raise PaymentRefused(reason="provider_unavailable", currency=currency) from exc


async def _mark_failed(session: AsyncSession, *, order_id: str, now: datetime.datetime) -> None:
    await session.execute(
        sa.update(Payment)
        .where(Payment.order_id == order_id)
        .values(status=PaymentStatus.FAILED.value, updated_at=now)
    )
    await session.commit()


async def _settle(
    session: AsyncSession,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    provider: PaymentProvider,
    order_id: str,
    provider_ref: str | None,
    mandate_ref: str | None,
    raw: dict[str, Any],
    paid: Money | None,
) -> bool:
    """Grant what the order bought, exactly once.

    The writing itself is services/subscriptions, because the reconciliation
    job settles the same orders when a callback is lost and the two must end in
    identical rows (app/workers/payments.py).
    """
    now = datetime.datetime.now(datetime.UTC)
    settlement = await settle_order(
        session,
        provider=provider,
        order_id=order_id,
        provider_ref=provider_ref,
        mandate_ref=mandate_ref,
        raw=raw,
        paid=paid,
        settings=settings,
        now=now,
    )
    if not settlement.granted:
        # Either no such order, or it was settled already. Both are answered
        # with 200 and neither grants anything a second time.
        await session.rollback()
        return False

    await session.commit()

    if settlement.plan == "pro" and settlement.user_id is not None:
        # Bought with the subscription, and a balance rather than a daily
        # counter, so it is granted rather than reset (C4).
        entitlements = Entitlements(session_factory=session_factory, settings=settings)
        await entitlements.grant_realtime_seconds(
            settlement.user_id, seconds=settings.limit_pro_realtime_seconds_monthly
        )

    log.info(
        "payments.subscription_activated",
        user_id=str(settlement.user_id),
        order_id=order_id,
        plan=settlement.plan,
        period=settlement.period,
        renewal_mode=settlement.renewal_mode,
    )
    return True


__all__ = ["router"]
