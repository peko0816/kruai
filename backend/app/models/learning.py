"""Attempts, mastery, progress. Mirrors docs/DATA_MODEL.sql section 学习."""

from __future__ import annotations

import datetime
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Attempt(Base):
    """One recorded utterance and its scores.

    Score columns are REAL because they are measurements in 0-100, not money.
    The money rule (integers only) applies to amount_minor and _usd_cents
    columns, which live in commerce.py.
    """

    __tablename__ = "attempts"
    __table_args__ = (
        sa.Index("idx_attempts_user_time", "user_id", sa.text("created_at DESC")),
        sa.Index("idx_attempts_concept", "user_id", "concept_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    lesson_item_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), sa.ForeignKey("lesson_items.id"), nullable=False
    )
    concept_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), sa.ForeignKey("concepts.id")
    )
    audio_url: Mapped[str | None] = mapped_column(sa.Text)
    asr_text: Mapped[str | None] = mapped_column(sa.Text)

    #: Composite score; the input to the mastery delta (PRD 9.2).
    pron_score: Mapped[float | None] = mapped_column(sa.REAL)
    accuracy_score: Mapped[float | None] = mapped_column(sa.REAL)
    fluency_score: Mapped[float | None] = mapped_column(sa.REAL)
    #: SCRIPTED mode only.
    completeness_score: Mapped[float | None] = mapped_column(sa.REAL)
    #: en-US only; Azure does not return prosody for zh-CN.
    prosody_score: Mapped[float | None] = mapped_column(sa.REAL)
    #: zh-CN only; derived or parsed per ZH_TONE_MODE.
    tone_score: Mapped[float | None] = mapped_column(sa.REAL)

    #: Provider raw response. Kept for triage, never read for business logic.
    phoneme_detail: Mapped[dict[str, Any] | None] = mapped_column(postgresql.JSONB)
    passed: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    provider: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )


class ConceptMastery(Base):
    """Per-user mastery plus SM-2 scheduling state (PRD 9.2)."""

    __tablename__ = "concept_mastery"
    __table_args__ = (sa.Index("idx_mastery_due", "user_id", "next_due_at", "mastery_score"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    concept_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), sa.ForeignKey("concepts.id"), primary_key=True
    )
    mastery_score: Mapped[float] = mapped_column(
        sa.REAL, nullable=False, server_default=sa.text("0")
    )
    attempt_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("0")
    )
    #: SM2_EASE_INITIAL. Floor is SM2_EASE_MIN, both configured.
    ease_factor: Mapped[float] = mapped_column(
        sa.REAL, nullable=False, server_default=sa.text("2.5")
    )
    interval_days: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("1")
    )
    last_seen_at: Mapped[datetime.datetime | None] = mapped_column(sa.TIMESTAMP(timezone=True))
    next_due_at: Mapped[datetime.datetime | None] = mapped_column(sa.TIMESTAMP(timezone=True))


class LessonProgress(Base):
    __tablename__ = "lesson_progress"
    __table_args__ = (
        sa.CheckConstraint(
            "status IN ('started','completed')", name="lesson_progress_status_check"
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    lesson_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), sa.ForeignKey("lessons.id"), primary_key=True
    )
    status: Mapped[str] = mapped_column(sa.Text, nullable=False)
    completed_at: Mapped[datetime.datetime | None] = mapped_column(sa.TIMESTAMP(timezone=True))


class Streak(Base):
    __tablename__ = "streaks"

    user_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    current_streak: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("0")
    )
    longest_streak: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("0")
    )
    #: Local calendar day, not a timestamp — a streak is a day count.
    last_day: Mapped[datetime.date | None] = mapped_column(sa.Date)


class Experiment(Base):
    """Stable A/B assignment. The S2-to-S3 gate is decided from this table."""

    __tablename__ = "experiments"

    user_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    experiment_key: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    variant: Mapped[str] = mapped_column(sa.Text, nullable=False)
    assigned_at: Mapped[datetime.datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )
