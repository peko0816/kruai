"""Allowance checks and deductions.

Every deduction is a single guarded UPDATE. Read the module docstring in
quota.py before adding an operation here — the rule against read-modify-write is
not a style preference, and the failure it prevents leaves no trace.
"""

from app.services.entitlements.quota import (
    UNLIMITED,
    Entitlements,
    EntitlementsMissingError,
    QuotaConsumption,
    QuotaSnapshot,
)

__all__ = [
    "UNLIMITED",
    "Entitlements",
    "EntitlementsMissingError",
    "QuotaConsumption",
    "QuotaSnapshot",
]
