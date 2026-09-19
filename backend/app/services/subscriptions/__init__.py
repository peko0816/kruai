"""Subscriptions: what a paid order grants, and when.

Domain layer, and one of the few here that touches the database — for the same
reason entitlements does (docs/DECISIONS.md D-018): the guarantee is "exactly
once", and that is a property of a SQL statement rather than of a calculation.

The acquirer is always passed in. This package may name payments/base.py and
nothing else under the adapter packages, which is what keeps a decision about
who settled a payment from turning into a decision about which vendor we use.
"""

from app.services.subscriptions.settle import (
    CALLBACK_FIELD,
    ORDER_DETAILS_FIELD,
    Settlement,
    activate,
    settle_order,
)

__all__ = [
    "CALLBACK_FIELD",
    "ORDER_DETAILS_FIELD",
    "Settlement",
    "activate",
    "settle_order",
]
