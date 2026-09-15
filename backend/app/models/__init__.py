"""SQLAlchemy models, one module per docs/DATA_MODEL.sql section.

Importing this package registers every table on ``Base.metadata``. Alembic's
env.py imports it for exactly that reason: a model module left out here would
silently disappear from autogenerate.
"""

from app.models.base import Base
from app.models.commerce import (
    CostLedger,
    Entitlement,
    Payment,
    RealtimeSession,
    Subscription,
)
from app.models.content import (
    Concept,
    ContentPack,
    Course,
    Lesson,
    LessonItem,
    MediaAsset,
)
from app.models.learning import (
    Attempt,
    ConceptMastery,
    Experiment,
    LessonProgress,
    Streak,
)
from app.models.orgs import Org, OrgMember, OrgReport
from app.models.users import User, UserProfile

__all__ = [
    "Attempt",
    "Base",
    "Concept",
    "ConceptMastery",
    "ContentPack",
    "CostLedger",
    "Course",
    "Entitlement",
    "Experiment",
    "Lesson",
    "LessonItem",
    "LessonProgress",
    "MediaAsset",
    "Org",
    "OrgMember",
    "OrgReport",
    "Payment",
    "RealtimeSession",
    "Streak",
    "Subscription",
    "User",
    "UserProfile",
]
