"""Users. Mirrors docs/DATA_MODEL.sql section 用户."""

from __future__ import annotations

import datetime
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )
    telegram_id: Mapped[int] = mapped_column(sa.BigInteger, unique=True, nullable=False)
    phone: Mapped[str | None] = mapped_column(sa.Text)
    #: km / zh / en
    locale: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default=sa.text("'km'"))
    created_at: Mapped[datetime.datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    last_active_at: Mapped[datetime.datetime | None] = mapped_column(sa.TIMESTAMP(timezone=True))
    #: Soft delete (CODING_STANDARDS section 8) — never a boolean flag.
    deleted_at: Mapped[datetime.datetime | None] = mapped_column(sa.TIMESTAMP(timezone=True))


class UserProfile(Base):
    __tablename__ = "user_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    target_language: Mapped[str] = mapped_column(
        sa.Text, nullable=False, server_default=sa.text("'zh'")
    )
    current_level: Mapped[str] = mapped_column(
        sa.Text, nullable=False, server_default=sa.text("'HSK1'")
    )
    daily_goal_minutes: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("10")
    )
    #: Drives the local-time quota reset (QUOTA_RESET_HOUR_LOCAL).
    timezone: Mapped[str] = mapped_column(
        sa.Text, nullable=False, server_default=sa.text("'Asia/Phnom_Penh'")
    )
    #: Data saver forces audio regardless of plan (PRD 7.3).
    data_saver: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("false")
    )
