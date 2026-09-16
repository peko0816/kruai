"""The payments fake is the one whose contract has teeth.

Two of its four guarantees are security properties rather than conveniences:
verify_callback is the boundary that decides whether a subscription gets
granted, and create_checkout's idempotency is what stops a retried request from
becoming a second charge. Both are asserted here against behaviour a caller can
observe, not against how they happen to be implemented.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.services.payments.base import (
    CheckoutRequest,
    Money,
    PaymentStatus,
    ProductKind,
)
from app.services.payments.fake import (
    FAILED_MARKER,
    PENDING_MARKER,
    SIGNATURE_HEADER,
    FakePaymentProvider,
    sign_payload,
)

USD = Money(amount_minor=199, currency="USD", currency_minor_units=2)
KHR = Money(amount_minor=8000, currency="KHR", currency_minor_units=0)


def request(
    order_id: str = "ord-1", *, money: Money = USD, recurring: bool = False
) -> CheckoutRequest:
    return CheckoutRequest(
        order_id=order_id,
        money=money,
        product_kind=ProductKind.SUBSCRIPTION,
        product_name="Basic monthly",
        user_ref="user-1",
        setup_recurring=recurring,
    )


def callback(payload: dict[str, Any], *, sign: bool = True) -> dict[str, Any]:
    """A callback as the acquirer would post it."""
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if sign:
        headers[SIGNATURE_HEADER] = sign_payload(body)
    return {"headers": headers, "body": body, "content_type": "application/json"}


SUCCEEDED = {
    "order_id": "ord-1",
    "provider_ref": "fakeref_x",
    "status": "succeeded",
    "amount_minor": 199,
    "currency": "USD",
    "currency_minor_units": 2,
}


# ------------------------------------------------------------- idempotency


async def test_repeating_a_checkout_returns_the_same_transaction() -> None:
    """BACKLOG B4 acceptance. A retried request must not become a second charge."""
    provider = FakePaymentProvider()
    first = await provider.create_checkout(request("ord-42"))
    second = await provider.create_checkout(request("ord-42"))
    assert first == second


async def test_idempotency_survives_a_new_provider_instance() -> None:
    """The registry hands out a fresh provider per call, so remembering the
    order would not have worked — this has to hold across instances."""
    first = await FakePaymentProvider().create_checkout(request("ord-42"))
    second = await FakePaymentProvider().create_checkout(request("ord-42"))
    assert first == second


async def test_different_orders_get_different_references() -> None:
    a = await FakePaymentProvider().create_checkout(request("ord-a"))
    b = await FakePaymentProvider().create_checkout(request("ord-b"))
    assert a.provider_ref != b.provider_ref


async def test_checkout_url_follows_the_documented_shape() -> None:
    result = await FakePaymentProvider().create_checkout(request("ord-7"))
    assert result.checkout_url == "fake://pay/ord-7"


# ------------------------------------------------------------ verify_callback


def test_a_valid_signature_is_accepted_and_parsed() -> None:
    result = FakePaymentProvider().verify_callback(**callback(SUCCEEDED))

    assert result.signature_valid is True
    assert result.order_id == "ord-1"
    assert result.status is PaymentStatus.SUCCEEDED
    assert result.money == USD


def test_a_forged_signature_is_rejected() -> None:
    forged = callback(SUCCEEDED)
    forged["headers"][SIGNATURE_HEADER] = "0" * 64
    assert FakePaymentProvider().verify_callback(**forged).signature_valid is False


def test_a_missing_signature_is_rejected() -> None:
    assert (
        FakePaymentProvider().verify_callback(**callback(SUCCEEDED, sign=False)).signature_valid
        is False
    )


def test_a_tampered_body_is_rejected() -> None:
    """The amount is the field an attacker would change."""
    signed = callback(SUCCEEDED)
    signed["body"] = json.dumps({**SUCCEEDED, "amount_minor": 1}).encode("utf-8")
    assert FakePaymentProvider().verify_callback(**signed).signature_valid is False


def test_a_rejected_callback_yields_no_usable_data() -> None:
    """Returning an order_id beside signature_valid=False invites a caller to
    use it without checking the flag — and grant a subscription on forged input."""
    forged = callback(SUCCEEDED)
    forged["headers"][SIGNATURE_HEADER] = "0" * 64
    result = FakePaymentProvider().verify_callback(**forged)

    assert result.order_id is None
    assert result.provider_ref is None
    assert result.money is None
    assert result.mandate_ref is None
    assert result.status is PaymentStatus.UNKNOWN
    assert result.raw == {}


def test_signature_header_is_matched_case_insensitively() -> None:
    """HTTP header names are case-insensitive; frameworks differ on casing."""
    signed = callback(SUCCEEDED)
    value = signed["headers"].pop(SIGNATURE_HEADER)
    signed["headers"]["X-Fake-Signature"] = value
    assert FakePaymentProvider().verify_callback(**signed).signature_valid is True


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded", ""])
def test_a_non_json_content_type_is_rejected(content_type: str) -> None:
    signed = callback(SUCCEEDED)
    signed["content_type"] = content_type
    assert FakePaymentProvider().verify_callback(**signed).signature_valid is False


@pytest.mark.parametrize("body", [b"not json", b"", b"[1,2,3]", b"\xff\xfe"])
def test_malformed_bodies_are_rejected_not_raised(body: bytes) -> None:
    headers = {"Content-Type": "application/json", SIGNATURE_HEADER: sign_payload(body)}
    result = FakePaymentProvider().verify_callback(
        headers=headers, body=body, content_type="application/json"
    )
    assert result.signature_valid is False


def test_verify_callback_has_no_side_effects() -> None:
    """Contract point 2. Verifying twice must be indistinguishable from once —
    the caller decides what to do, this only reports."""
    provider = FakePaymentProvider()
    first = provider.verify_callback(**callback(SUCCEEDED))
    second = provider.verify_callback(**callback(SUCCEEDED))
    assert first == second


def test_an_unknown_status_string_does_not_become_succeeded() -> None:
    """Defaulting an unrecognised status to success would be the worst failure."""
    result = FakePaymentProvider().verify_callback(**callback({**SUCCEEDED, "status": "whatever"}))
    assert result.signature_valid is True
    assert result.status is PaymentStatus.UNKNOWN


@pytest.mark.parametrize(
    "overrides",
    [
        {"amount_minor": "199"},
        {"amount_minor": 1.99},
        {"amount_minor": True},
        {"currency": "EUR"},
        {"currency_minor_units": 2, "currency": "KHR"},
        {"currency_minor_units": "2"},
    ],
)
def test_incoherent_amounts_come_back_as_no_money(overrides: dict[str, Any]) -> None:
    """A KHR amount claiming two decimal places is off by 100x; better absent
    than wrong, and the caller sees money=None rather than a plausible lie."""
    result = FakePaymentProvider().verify_callback(**callback({**SUCCEEDED, **overrides}))
    assert result.signature_valid is True
    assert result.money is None


def test_khr_callbacks_parse_with_zero_minor_units() -> None:
    payload = {
        **SUCCEEDED,
        "amount_minor": 8000,
        "currency": "KHR",
        "currency_minor_units": 0,
    }
    assert FakePaymentProvider().verify_callback(**callback(payload)).money == KHR


# ----------------------------------------------------- rejected checkouts


@pytest.mark.parametrize(
    ("money", "expected"),
    [
        (
            Money(amount_minor=0, currency="USD", currency_minor_units=2),
            "payment.non_positive_amount",
        ),
        (
            Money(amount_minor=-199, currency="USD", currency_minor_units=2),
            "payment.non_positive_amount",
        ),
        (Money(amount_minor=8000, currency="KHR", currency_minor_units=2), "payment.invalid_money"),
        (Money(amount_minor=100, currency="EUR", currency_minor_units=2), "payment.invalid_money"),
    ],
)
async def test_bad_money_is_refused_with_a_named_code(money: Money, expected: str) -> None:
    result = await FakePaymentProvider().create_checkout(request(money=money))
    assert result.ok is False
    assert result.error_code == expected


async def test_an_empty_order_id_is_refused() -> None:
    result = await FakePaymentProvider().create_checkout(request("  "))
    assert result.ok is False
    assert result.error_code == "payment.order_id_required"


async def test_a_currency_this_channel_cannot_settle_is_refused() -> None:
    manual = FakePaymentProvider(name="fake_manual", supported_currencies=("USD",))
    result = await manual.create_checkout(request(money=KHR))
    assert result.ok is False
    assert result.error_code == "payment.currency_unsupported"


# --------------------------------------------------------------- KHQR branch


async def test_khr_checkouts_carry_a_qr_payload() -> None:
    """ARCHITECTURE 3.3 lists QR-versus-redirect as one of the three acquirer
    differences the design absorbs; nothing would exercise it otherwise."""
    result = await FakePaymentProvider().create_checkout(request(money=KHR))
    assert result.qr_payload is not None
    assert result.checkout_url is not None


async def test_usd_checkouts_are_redirect_only() -> None:
    result = await FakePaymentProvider().create_checkout(request(money=USD))
    assert result.qr_payload is None


# ------------------------------------------------------- the two renewal paths


async def test_a_recurring_channel_issues_a_mandate_when_asked() -> None:
    result = await FakePaymentProvider().create_checkout(request(recurring=True))
    assert result.mandate_ref is not None


async def test_no_mandate_appears_unless_it_was_requested() -> None:
    result = await FakePaymentProvider().create_checkout(request(recurring=False))
    assert result.mandate_ref is None


async def test_a_manual_channel_ignores_the_request_rather_than_failing() -> None:
    """base.py: a provider that cannot do this should ignore setup_recurring and
    return mandate_ref=None, not error."""
    manual = FakePaymentProvider(name="fake_manual", supports_recurring=False)
    result = await manual.create_checkout(request(recurring=True))

    assert result.ok is True
    assert result.mandate_ref is None


async def test_a_recurring_channel_can_charge_its_mandate() -> None:
    result = await FakePaymentProvider().charge_recurring(
        mandate_ref="fakemandate_x", order_id="ord-renew", money=USD, product_name="Basic"
    )
    assert result.ok is True
    assert result.status is PaymentStatus.SUCCEEDED


async def test_a_manual_channel_refuses_to_charge() -> None:
    """The business layer must branch on supports_recurring; this is what it
    gets if it does not."""
    manual = FakePaymentProvider(name="fake_manual", supports_recurring=False)
    result = await manual.charge_recurring(
        mandate_ref="whatever", order_id="ord-renew", money=USD, product_name="Basic"
    )
    assert result.ok is False
    assert result.error_code == "recurring.unsupported"


async def test_a_declined_charge_is_reported_not_raised() -> None:
    """D8b retries on failure; that branch needs to be reachable."""
    result = await FakePaymentProvider().charge_recurring(
        mandate_ref="fakemandate_x",
        order_id=f"ord-{FAILED_MARKER}",
        money=USD,
        product_name="Basic",
    )
    assert result.ok is False
    assert result.status is PaymentStatus.FAILED


async def test_charging_without_a_mandate_is_refused() -> None:
    result = await FakePaymentProvider().charge_recurring(
        mandate_ref="", order_id="ord-renew", money=USD, product_name="Basic"
    )
    assert result.ok is False
    assert result.error_code == "payment.mandate_required"


# -------------------------------------------------------------- query_status


@pytest.mark.parametrize(
    ("order_id", "expected"),
    [
        ("ord-1", PaymentStatus.SUCCEEDED),
        (f"ord-{PENDING_MARKER}", PaymentStatus.PENDING),
        (f"ord-{FAILED_MARKER}", PaymentStatus.FAILED),
    ],
)
async def test_status_lookup_reaches_every_branch(order_id: str, expected: PaymentStatus) -> None:
    assert await FakePaymentProvider().query_status(order_id) == expected


# -------------------------------------------------------------- declarations


def test_capabilities_are_declared_honestly() -> None:
    """Contract point 4: the business layer branches on these."""
    recurring = FakePaymentProvider()
    manual = FakePaymentProvider(
        name="fake_manual", supports_recurring=False, supported_currencies=("USD",)
    )

    assert recurring.supports_recurring is True
    assert "KHR" in recurring.supported_currencies
    assert manual.supports_recurring is False
    assert "KHR" not in manual.supported_currencies


async def test_cancelling_a_mandate_succeeds_by_default() -> None:
    assert await FakePaymentProvider().cancel_mandate(mandate_ref="fakemandate_x") is True
