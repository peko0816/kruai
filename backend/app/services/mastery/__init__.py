"""Concept mastery and its review schedule.

The domain layer, so nothing here does IO or imports a provider implementation
(ARCHITECTURE section 1). Everything takes values and Settings and returns
values, which is what keeps the product's central judgement — is this learner
getting better — testable without a database or a vendor.

C1 lands the per-attempt delta; SM-2 scheduling and the review queue follow in
their own entries.
"""

from app.services.mastery.delta import (
    MASTERY_CEILING,
    MASTERY_FLOOR,
    MasteryUpdate,
    ScorableItemType,
    apply_attempt,
    clamp_mastery,
    is_passing,
    score_delta,
    weight_for,
)
from app.services.mastery.review import (
    REVIEW_DURATION_CHOICES,
    ReviewCandidate,
    ReviewQueue,
    build_review_queue,
    due_candidates,
)
from app.services.mastery.sm2 import (
    INITIAL_INTERVAL_DAYS,
    ReviewSchedule,
    ScheduleBand,
    initial_ease_factor,
    schedule_review,
)

__all__ = [
    "INITIAL_INTERVAL_DAYS",
    "MASTERY_CEILING",
    "MASTERY_FLOOR",
    "REVIEW_DURATION_CHOICES",
    "MasteryUpdate",
    "ReviewCandidate",
    "ReviewQueue",
    "ReviewSchedule",
    "ScheduleBand",
    "ScorableItemType",
    "apply_attempt",
    "build_review_queue",
    "clamp_mastery",
    "due_candidates",
    "initial_ease_factor",
    "is_passing",
    "schedule_review",
    "score_delta",
    "weight_for",
]
