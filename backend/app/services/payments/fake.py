"""Deterministic stand-in for a payment acquirer.

Registered twice, because the two renewal paths in ARCHITECTURE 3.4 are decided
by ``supports_recurring`` and the business layer is forbidden from assuming
either. ``fake`` can take a mandate and charge it; ``fake_manual`` cannot, and
falls back to the base class's refusal. That mirrors the real split — ABA PayWay
can do direct debit, Telegram Stars cannot — so D8b can drive both without
config gymnastics.

Idempotency is by construction rather than by memory. The registry hands out a
fresh provider per call, so a remembered order_id would be forgotten
immediately; every field of a checkout is instead derived from the request, and
calling twice with the same order_id therefore cannot produce anything
different.

The HMAC key below is published on purpose — tests have to be able to produce a
valid signature. That is also why registry.py refuses to build these providers
when ENV=prod: anyone reading this file could otherwise forge a callback and
grant themselves a subscription.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Final

from app.core.money import is_supported_currency, validate_money
from app.services.payments.base import (
    CallbackResult,
    ChargeResult,
    CheckoutRequest,
    CheckoutResult,
    Money,
    PaymentProvider,
    PaymentStatus,
)

#: A published test fixture, not a credential: the signing scheme is only
#: useful here if a test can sign with it. A real adapter reads its key from
#: Settings, which is what R5 is about.
FAKE_HMAC_KEY: Final = b"kruai-fake-payment-provider-signing-key"

#: Where the fake looks for the signature. A real acquirer may instead put a
#: hash inside the body (ABA PayWay does, with a fixed field order — BACKLOG
#: D9); verify_callback receives headers, body and content_type so either shape
#: fits behind the same abstraction.
SIGNATURE_HEADER: Final = "x-fake-signature"

#: Markers inside an order_id that steer query_status, so reconciliation
#: branches are reachable without a broken acquirer. Same idea as FakeScorer's
#: __BAD__.
PENDING_MARKER: Final = "__PENDING__"
FAILED_MARKER: Final = "__FAILED__"


def sign_payload(body: bytes) -> str:
    """Signature the fake will accept for ``body``.

    Exported so tests and the D8a webhook integration can build a genuine
    callback instead of reimplementing the scheme.
    """
    return hmac.new(FAKE_HMAC_KEY, body, hashlib.sha256).hexdigest()


def _provider_ref(order_id: str) -> str:
    """Stable acquirer-side reference. Derived, so it survives a restart."""
    digest = hashlib.sha256(order_id.encode("utf-8")).hexdigest()[:16]
    return f"fakeref_{digest}"


def _khqr_payload(request: CheckoutRequest) -> str:
    """Stand-in for a KHQR string. The client renders it as a QR code.

    Present so the qr_payload branch of CheckoutResult is exercised under an
    all-fake configuration; ARCHITECTURE 3.3 lists it as one of the three
    acquirer differences the design has to absorb.
    """
    return f"fakeqr|{request.order_id}|{request.money.amount_minor}|{request.money.currency}"


class FakePaymentProvider(PaymentProvider):
    """Signs, verifies and reports. Never touches a network or a database."""

    def __init__(
        self,
        *,
        name: str = "fake",
        supports_recurring: bool = True,
        supported_currencies: tuple[str, ...] = ("USD", "KHR"),
    ) -> None:
        self.name = name
        self.supports_recurring = supports_recurring
        self.supported_currencies = supported_currencies

    # ------------------------------------------------------------- checkout

    async def create_checkout(self, req: CheckoutRequest) -> CheckoutResult:
        rejection = self._reject(req)
        if rejection is not None:
            return rejection

        return CheckoutResult(
            ok=True,
            checkout_url=f"fake://pay/{req.order_id}",
            qr_payload=_khqr_payload(req) if req.money.currency == "KHR" else None,
            provider_ref=_provider_ref(req.order_id),
            # Only when both sides agree: the caller asked, and this channel can.
            mandate_ref=(
                f"fakemandate_{_provider_ref(req.order_id)}"
                if req.setup_recurring and self.supports_recurring
                else None
            ),
            # Deliberately absent. A wall-clock expiry would differ between two
            # calls for the same order_id and break the idempotency the contract
            # requires; a derived one would be an arbitrary instant. A fake
            # checkout simply does not expire.
            expires_at_epoch=None,
        )

    def _reject(self, req: CheckoutRequest) -> CheckoutResult | None:
        """Everything that would make a real acquirer refuse the order."""
        if not req.order_id.strip():
            return _checkout_failure("payment.order_id_required", "order_id is empty")

        try:
            # Catches a KHR amount declaring two decimal places, which is the
            # bug the amount_minor convention exists to prevent.
            validate_money(req.money)
        except (TypeError, ValueError) as exc:
            return _checkout_failure("payment.invalid_money", str(exc))

        if req.money.amount_minor <= 0:
            return _checkout_failure(
                "payment.non_positive_amount", f"amount_minor is {req.money.amount_minor}"
            )
        if req.money.currency not in self.supported_currencies:
            return _checkout_failure(
                "payment.currency_unsupported",
                f"{self.name} settles {list(self.supported_currencies)}, "
                f"asked for {req.money.currency}",
            )
        return None

    # ------------------------------------------------------------- callback

    def verify_callback(
        self, *, headers: dict[str, str], body: bytes, content_type: str
    ) -> CallbackResult:
        """Verify and parse. No IO, no writes — this is the security boundary.

        On a bad signature nothing is parsed out. Handing back an order_id
        beside signature_valid=False invites a caller to use it without checking
        the flag, and that caller would be granting subscriptions on unverified
        input.
        """
        if "json" not in content_type.lower():
            return CallbackResult(signature_valid=False)

        provided = _header(headers, SIGNATURE_HEADER)
        if provided is None or not hmac.compare_digest(provided, sign_payload(body)):
            return CallbackResult(signature_valid=False)

        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return CallbackResult(signature_valid=False)
        if not isinstance(payload, dict):
            return CallbackResult(signature_valid=False)

        return CallbackResult(
            signature_valid=True,
            order_id=_as_str(payload.get("order_id")),
            provider_ref=_as_str(payload.get("provider_ref")),
            status=_as_status(payload.get("status")),
            money=_as_money(payload),
            mandate_ref=_as_str(payload.get("mandate_ref")),
            raw=payload,
        )

    # -------------------------------------------------------------- queries

    async def query_status(self, order_id: str) -> PaymentStatus:
        """Reconciliation fallback for a lost callback.

        Markers in the order_id select the branch, so a caller can reach the
        pending and failed paths without an acquirer that actually fails.
        """
        if PENDING_MARKER in order_id:
            return PaymentStatus.PENDING
        if FAILED_MARKER in order_id:
            return PaymentStatus.FAILED
        return PaymentStatus.SUCCEEDED

    # ------------------------------------------------------------ recurring

    async def charge_recurring(
        self, *, mandate_ref: str, order_id: str, money: Money, product_name: str
    ) -> ChargeResult:
        """Only overridden on the recurring-capable instance.

        ``fake_manual`` inherits the base class's refusal, which is what lets a
        test prove the business layer checks supports_recurring instead of
        relying on the call failing.
        """
        if not self.supports_recurring:
            return await super().charge_recurring(
                mandate_ref=mandate_ref,
                order_id=order_id,
                money=money,
                product_name=product_name,
            )
        if not mandate_ref:
            return ChargeResult(
                ok=False,
                status=PaymentStatus.FAILED,
                error_code="payment.mandate_required",
                error_message="mandate_ref is empty",
            )
        if FAILED_MARKER in order_id:
            return ChargeResult(
                ok=False,
                status=PaymentStatus.FAILED,
                error_code="payment.charge_declined",
                error_message="declined by the acquirer",
            )
        return ChargeResult(
            ok=True, status=PaymentStatus.SUCCEEDED, provider_ref=_provider_ref(order_id)
        )


def _header(headers: dict[str, str], name: str) -> str | None:
    """HTTP header names are case-insensitive; a dict's keys are not."""
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _as_status(value: Any) -> PaymentStatus:
    try:
        return PaymentStatus(value)
    except ValueError:
        return PaymentStatus.UNKNOWN


def _as_money(payload: dict[str, Any]) -> Money | None:
    """Only build Money when the acquirer sent a coherent, known amount."""
    amount = payload.get("amount_minor")
    currency = payload.get("currency")
    if not isinstance(amount, int) or isinstance(amount, bool):
        return None
    if not isinstance(currency, str) or not is_supported_currency(currency):
        return None

    units = payload.get("currency_minor_units")
    if not isinstance(units, int) or isinstance(units, bool):
        return None

    money = Money(amount_minor=amount, currency=currency, currency_minor_units=units)
    try:
        validate_money(money)
    except (TypeError, ValueError):
        return None
    return money


def _checkout_failure(code: str, message: str) -> CheckoutResult:
    return CheckoutResult(ok=False, error_code=code, error_message=message)


__all__ = [
    "FAILED_MARKER",
    "FAKE_HMAC_KEY",
    "PENDING_MARKER",
    "SIGNATURE_HEADER",
    "FakePaymentProvider",
    "sign_payload",
]
