"""What every payment provider must do, whoever wrote it.

``services/payments/base.py`` states four promises in prose — idempotent
checkout, a side-effect-free verify, no exceptions, honest capabilities — and
prose is not enforceable. This file turns them into tests that run against
*every* provider the registry knows, so an adapter added later is held to them
from the moment it is registered rather than from the moment somebody
remembers.

That is most of the point of writing it now: the ABA adapter (BACKLOG D9) is
blocked on credentials nobody has yet, and when it lands its author should
discover any contract breach here rather than in a learner's failed payment.
Adding a provider means adding a line to the registry; these tests then apply
to it with no further work.

What cannot be checked here is what a real acquirer does with a real request.
These are the properties that hold without a network — and they are exactly the
ones that are easy to get wrong quietly.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.config import Settings
from app.core.money import is_supported_currency, minor_units_for
from app.services.payments.base import (
    CheckoutRequest,
    Money,
    PaymentProvider,
    PaymentStatus,
    ProductKind,
)
from app.services.payments.registry import available_providers, build_payment_provider

_MINIMAL: dict[str, Any] = {
    "DATABASE_URL": "postgresql+psycopg://u:p@localhost:5432/db",
    "REDIS_URL": "redis://localhost:6379/0",
    "TELEGRAM_BOT_TOKEN": "",
    "AZURE_SPEECH_KEY": "",
    "AZURE_SPEECH_REGION": "",
    "GOOGLE_APPLICATION_CREDENTIALS": "",
    "ELEVENLABS_API_KEY": "",
    "OPENAI_API_KEY": "",
    "PAYWAY_MERCHANT_ID": "",
    "PAYWAY_API_KEY": "",
    "PAYWAY_BASE_URL": "",
    "BAKONG_TOKEN": "",
    "JWT_SECRET": "",
}


def settings() -> Settings:
    return Settings(_env_file=None, **_MINIMAL)


@pytest.fixture(params=available_providers())
def provider(request: pytest.FixtureRequest) -> PaymentProvider:
    """Every registered provider in turn, including ones added later."""
    return build_payment_provider(request.param, settings=settings())


def order(provider: PaymentProvider, *, order_id: str = "kruai_contract") -> CheckoutRequest:
    currency = provider.supported_currencies[0]
    return CheckoutRequest(
        order_id=order_id,
        money=Money(
            amount_minor=199,
            currency=currency,
            currency_minor_units=minor_units_for(currency),
        ),
        product_kind=ProductKind.SUBSCRIPTION,
        product_name="basic-monthly",
        user_ref="7b1f0c9e-0000-4000-8000-000000000000",
    )


# ------------------------------------------------------- declared capabilities


def test_a_provider_names_itself(provider: PaymentProvider) -> None:
    """The name reaches payments.provider and the cost dashboard."""
    assert isinstance(provider.name, str)
    assert provider.name.strip()


def test_a_provider_declares_whether_it_can_charge_again(provider: PaymentProvider) -> None:
    """The business layer branches on this and must not have to guess
    (ARCHITECTURE 3.4)."""
    assert isinstance(provider.supports_recurring, bool)


def test_a_provider_declares_currencies_this_project_can_store(
    provider: PaymentProvider,
) -> None:
    """A currency with no declared minor-unit count cannot be stored safely:
    that is how a riel amount ends up read as cents."""
    assert provider.supported_currencies
    for currency in provider.supported_currencies:
        assert is_supported_currency(currency), f"{provider.name} settles unknown {currency}"


# ------------------------------------------------------------------- checkout


async def test_checkout_is_idempotent(provider: PaymentProvider) -> None:
    """Promise 1. A retried checkout must not become a second order — the
    order id is the idempotency key and the acquirer's answer has to agree."""
    first = await provider.create_checkout(order(provider))
    second = await provider.create_checkout(order(provider))

    assert first == second


async def test_checkout_offers_somewhere_to_pay(provider: PaymentProvider) -> None:
    """A success with neither a URL nor a QR payload is a success nobody can
    act on (ARCHITECTURE 3.3 names both shapes)."""
    result = await provider.create_checkout(order(provider))

    assert result.ok
    assert result.checkout_url or result.qr_payload


async def test_checkout_refuses_rather_than_raises(provider: PaymentProvider) -> None:
    """Promise 3. A refusal is a value, so the caller can choose what to do."""
    broken = CheckoutRequest(
        order_id="",
        money=Money(amount_minor=-1, currency="USD", currency_minor_units=2),
        product_kind=ProductKind.SUBSCRIPTION,
        product_name="",
        user_ref="",
    )

    result = await provider.create_checkout(broken)

    assert result.ok is False
    assert result.error_code


async def test_checkout_refuses_a_currency_it_cannot_settle(
    provider: PaymentProvider,
) -> None:
    unsettleable = next((c for c in ("USD", "KHR") if c not in provider.supported_currencies), None)
    if unsettleable is None:
        pytest.skip(f"{provider.name} settles everything this project knows about")

    result = await provider.create_checkout(
        CheckoutRequest(
            order_id="kruai_contract_currency",
            money=Money(
                amount_minor=199,
                currency=unsettleable,
                currency_minor_units=minor_units_for(unsettleable),
            ),
            product_kind=ProductKind.SUBSCRIPTION,
            product_name="basic-monthly",
            user_ref="u",
        )
    )

    assert result.ok is False


async def test_a_mandate_is_only_returned_when_both_sides_agree(
    provider: PaymentProvider,
) -> None:
    """Asking a channel that cannot do direct debit is not an error — it
    returns no mandate and the business layer goes down the manual path."""
    result = await provider.create_checkout(
        CheckoutRequest(
            order_id="kruai_contract_mandate",
            money=order(provider).money,
            product_kind=ProductKind.SUBSCRIPTION,
            product_name="basic-monthly",
            user_ref="u",
            setup_recurring=True,
        )
    )

    assert result.ok
    if provider.supports_recurring:
        assert result.mandate_ref
    else:
        assert result.mandate_ref is None


# ------------------------------------------------------------------- callback


def test_verify_refuses_an_unsigned_callback(provider: PaymentProvider) -> None:
    """The security boundary. Everything downstream trusts this answer."""
    result = provider.verify_callback(
        headers={},
        body=b'{"order_id": "kruai_1", "status": "succeeded"}',
        content_type="application/json",
    )

    assert result.signature_valid is False


def test_a_refused_callback_hands_back_nothing_to_act_on(
    provider: PaymentProvider,
) -> None:
    """Promise 2, the half that matters: returning an order_id beside
    signature_valid=False invites a caller to use it without checking."""
    result = provider.verify_callback(
        headers={"x-fake-signature": "0" * 64},
        body=b'{"order_id": "kruai_1", "status": "succeeded", "amount_minor": 199}',
        content_type="application/json",
    )

    assert result.signature_valid is False
    assert result.order_id is None
    assert result.money is None
    assert result.status is PaymentStatus.UNKNOWN


def test_verify_refuses_rather_than_raises(provider: PaymentProvider) -> None:
    """Whatever arrives at a public endpoint, it is not the acquirer's fault
    and it must not be a traceback."""
    for body in (b"", b"not json", b"[]", b"\xff\xfe", b"null"):
        result = provider.verify_callback(headers={}, body=body, content_type="application/json")
        assert result.signature_valid is False


async def test_query_status_answers_for_an_unknown_order(
    provider: PaymentProvider,
) -> None:
    """The reconciliation fallback has to answer something for every order it
    is asked about, including ones the acquirer has never seen."""
    assert isinstance(await provider.query_status("kruai_never_existed"), PaymentStatus)


# ------------------------------------------------------------------ recurring


async def test_a_channel_that_cannot_charge_again_says_so(
    provider: PaymentProvider,
) -> None:
    """And says it as a value, not an exception: the caller checks
    supports_recurring and must never rely on a failure to find out."""
    if provider.supports_recurring:
        pytest.skip(f"{provider.name} supports recurring charges")

    result = await provider.charge_recurring(
        mandate_ref="whatever",
        order_id="kruai_r_contract",
        money=order(provider).money,
        product_name="basic-monthly",
    )

    assert result.ok is False
    assert result.error_code == "recurring.unsupported"


async def test_a_recurring_charge_without_a_mandate_is_refused(
    provider: PaymentProvider,
) -> None:
    if not provider.supports_recurring:
        pytest.skip(f"{provider.name} has no recurring path")

    result = await provider.charge_recurring(
        mandate_ref="",
        order_id="kruai_r_contract",
        money=order(provider).money,
        product_name="basic-monthly",
    )

    assert result.ok is False
