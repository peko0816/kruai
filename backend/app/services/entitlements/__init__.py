"""Allowance checks, deductions and their daily reset.

Every deduction is a single guarded UPDATE. Read the module docstring in
quota.py before adding an operation here — the rule against read-modify-write is
not a style preference, and the failure it prevents leaves no trace.

Resets are per-learner local time, which brings its own set of quiet failures;
reset.py explains those.

plan.py answers the prior question — which plan a learner is on at all — by
reading their subscriptions. It reads ``status`` and does not re-derive it from
the dates; that state machine belongs to the renewal task (ARCHITECTURE 3.4).
"""

from app.services.entitlements.cost_guard import (
    alert_threshold_usd_cents,
    is_over_threshold,
    month_start,
    monthly_spend_usd_cents,
)
from app.services.entitlements.plan import (
    ENTITLING_STATUSES,
    FREE_PLAN,
    best_plan,
    current_plan,
)
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
    "ENTITLING_STATUSES",
    "FREE_PLAN",
    "UNLIMITED",
    "Entitlements",
    "EntitlementsMissingError",
    "QuotaConsumption",
    "QuotaReset",
    "QuotaSnapshot",
    "ResetOutcome",
    "alert_threshold_usd_cents",
    "best_plan",
    "current_plan",
    "is_over_threshold",
    "load_timezone",
    "month_start",
    "monthly_spend_usd_cents",
    "next_reset_at",
]
