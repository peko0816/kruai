"""Allowance checks, deductions and their daily reset.

Every deduction is a single guarded UPDATE. Read the module docstring in
quota.py before adding an operation here — the rule against read-modify-write is
not a style preference, and the failure it prevents leaves no trace.

Resets are per-learner local time, which brings its own set of quiet failures;
reset.py explains those.
"""

from app.services.entitlements.quota import (
    UNLIMITED,
    Entitlements,
    EntitlementsMissingError,
    QuotaConsumption,
    QuotaSnapshot,
)
from app.services.entitlements.reset import (
    QuotaReset,
    ResetOutcome,
    load_timezone,
    next_reset_at,
)

__all__ = [
    "UNLIMITED",
    "Entitlements",
    "EntitlementsMissingError",
    "QuotaConsumption",
    "QuotaReset",
    "QuotaSnapshot",
    "ResetOutcome",
    "load_timezone",
    "next_reset_at",
]
