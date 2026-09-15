"""Subscriptions, payments, cost. Mirrors docs/DATA_MODEL.sql section 商业.

Two money conventions live here and must not be mixed:

  · User-facing amounts: ``amount_minor`` INTEGER plus ``currency`` and
    ``currency_minor_units``. Never "cents" — KHR has no subunit.
  · Internal cost accounting: always USD, column names end ``_usd_cents``.

Both are integers. A float or numeric column for money would be a red-line
violation (CLAUDE.md R4).
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Subscription(Base):
    """One table serves both renewal paths (ARCHITECTURE 3.4).

    ``renewal_mode`` follows from ``provider.supports_recurring``: auto keeps a
    mandate and a next_charge_at, manual keeps a reminder timestamp. The
    business layer must never assume one of them.
    """

    __tablename__ = "subscriptions"
    __table_args__ = (
        sa.CheckConstraint("plan IN ('free','basic','pro')", name="subscriptions_plan_check"),
        sa.CheckConstraint(
            "status IN ('active','grace','expired','cancelled')",
            name="subscriptions_status_check",
        ),
        sa.CheckConstraint(
            "renewal_mode IN ('auto','manual')", name="subscriptions_renewal_mode_check"
        ),
        sa.CheckConstraint(
            "renewal_mode <> 'auto' OR mandate_ref IS NOT NULL", name="auto_needs_mandate"
        ),
        sa.Index("idx_sub_user_active", "user_id", "status", sa.text("period_end DESC")),
        # Partial indexes drive the two renewal cron jobs.
        sa.Index(
            "idx_sub_due_auto",
            "next_charge_at",
            postgresql_where=sa.text("renewal_mode = 'auto' AND status = 'active'"),
        ),
        sa.Index(
            "idx_sub_due_manual",
            "period_end",
            postgresql_where=sa.text("renewal_mode = 'manual' AND status = 'active'"),
        ),
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
    plan: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False)
    renewal_mode: Mapped[str] = mapped_column(
        sa.Text, nullable=False, server_default=sa.text("'manual'")
    )
    period_start: Mapped[datetime.datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False
    )
    period_end: Mapped[datetime.datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False
    )
    #: Entitlements stay live through grace; expiry downgrades to free limits.
    grace_until: Mapped[datetime.datetime | None] = mapped_column(sa.TIMESTAMP(timezone=True))
    payment_provider: Mapped[str | None] = mapped_column(sa.Text)
    external_ref: Mapped[str | None] = mapped_column(sa.Text)
    #: Required when renewal_mode='auto' (enforced by auto_needs_mandate).
    mandate_ref: Mapped[str | None] = mapped_column(sa.Text)
    next_charge_at: Mapped[datetime.datetime | None] = mapped_column(sa.TIMESTAMP(timezone=True))
    #: manual mode only; prevents duplicate renewal reminders.
    reminder_sent_at: Mapped[datetime.datetime | None] = mapped_column(sa.TIMESTAMP(timezone=True))
    cancelled_at: Mapped[datetime.datetime | None] = mapped_column(sa.TIMESTAMP(timezone=True))


class Entitlement(Base):
    """Live allowance. Decrements must be atomic UPDATE ... WHERE, never read-modify-write."""

    __tablename__ = "entitlements"
    __table_args__ = (
        sa.CheckConstraint("realtime_seconds_remaining >= 0", name="realtime_non_negative"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    #: Seconds, not minutes — minutes are a display unit only.
    realtime_seconds_remaining: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("0")
    )
    daily_attempts_used: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("0")
    )
    daily_tasks_used: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("0")
    )
    #: Next reset instant, computed from the user's timezone.
    reset_at: Mapped[datetime.datetime] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=False)


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
    )
    #: Our order number and the idempotency key for create_checkout.
    order_id: Mapped[str] = mapped_column(sa.Text, unique=True, nullable=False)
    #: Plain text, not an enum: swapping acquirer must not need a migration.
    provider: Mapped[str] = mapped_column(sa.Text, nullable=False)
    provider_ref: Mapped[str | None] = mapped_column(sa.Text)

    #: Smallest unit of `currency`. USD 1.99 -> 199; KHR 8000 -> 8000.
    amount_minor: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    currency: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default=sa.text("'USD'"))
    #: USD=2, KHR=0.
    currency_minor_units: Mapped[int] = mapped_column(
        sa.SmallInteger, nullable=False, server_default=sa.text("2")
    )

    is_recurring_charge: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("false")
    )
    status: Mapped[str] = mapped_column(sa.Text, nullable=False)
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(postgresql.JSONB)
    created_at: Mapped[datetime.datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )


class RealtimeSession(Base):
    __tablename__ = "realtime_sessions"
    __table_args__ = (sa.Index("idx_rt_user_time", "user_id", sa.text("started_at DESC")),)

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
    started_at: Mapped[datetime.datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    ended_at: Mapped[datetime.datetime | None] = mapped_column(sa.TIMESTAMP(timezone=True))
    seconds_billed: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("0")
    )
    cost_usd_cents_est: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("0")
    )
    transcript_url: Mapped[str | None] = mapped_column(sa.Text)


class CostLedger(Base):
    """Every external API call leaves a row here (CLAUDE.md section 8).

    An unledgered call is treated as not having happened, so this is the table
    /admin/costs aggregates and the cost guardrails read.
    """

    __tablename__ = "cost_ledger"
    __table_args__ = (
        sa.Index("idx_cost_time", sa.text("occurred_at DESC")),
        sa.Index("idx_cost_user_month", "user_id", "occurred_at"),
    )

    #: BIGSERIAL: this table grows fastest and is append-only.
    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime.datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    #: Null for content-production calls, which are not attributable to a user.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    provider: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: seconds / tokens / calls / minutes
    unit: Mapped[str] = mapped_column(sa.Text, nullable=False)
    quantity: Mapped[float] = mapped_column(sa.REAL, nullable=False)
    cost_usd_cents_est: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    #: 'attempt' / 'realtime' / 'content_production'
    ref: Mapped[str | None] = mapped_column(sa.Text)
