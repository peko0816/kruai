"""Payments.

``Money`` lives here, not in core. interfaces/payments_base.py is normative and
already defines it, so core/money.py deliberately holds only the currency
metadata and formatting and works against a structural protocol that this class
satisfies (docs/DECISIONS.md D-003). Import Money from here; import formatting
from core.money.

Exports the registry and base types only, never a concrete implementation
(ARCHITECTURE section 3).
"""

from app.services.payments.base import (
    CallbackResult,
    ChargeResult,
    CheckoutRequest,
    CheckoutResult,
    Money,
    PaymentProvider,
    PaymentStatus,
    ProductKind,
    RenewalMode,
)
from app.services.payments.registry import (
    available_providers,
    build_payment_provider,
    enabled_providers,
    get_payment_provider,
    provider_for_currency,
)

__all__ = [
    "CallbackResult",
    "ChargeResult",
    "CheckoutRequest",
    "CheckoutResult",
    "Money",
    "PaymentProvider",
    "PaymentStatus",
    "ProductKind",
    "RenewalMode",
    "available_providers",
    "build_payment_provider",
    "enabled_providers",
    "get_payment_provider",
    "provider_for_currency",
]
