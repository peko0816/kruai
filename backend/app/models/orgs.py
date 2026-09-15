"""B2B entities. Mirrors docs/DATA_MODEL.sql section B 端.

Tables land in M0-unblocked work now so the schema is complete in one
migration; the features that read them belong to M4 (BACKLOG stage G).
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Org(Base):
    __tablename__ = "orgs"

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    contact: Mapped[str | None] = mapped_column(sa.Text)
    plan: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default=sa.text("'standard'"))
    seats: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("0"))
    created_at: Mapped[datetime.datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )


class OrgMember(Base):
    __tablename__ = "org_members"

    org_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("orgs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    job_role: Mapped[str | None] = mapped_column(sa.Text)
    enrolled_at: Mapped[datetime.datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )


class OrgReport(Base):
    """Monthly PDF report, generated on the 1st for every active org."""

    __tablename__ = "org_reports"
    __table_args__ = (sa.UniqueConstraint("org_id", "period"),)

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("orgs.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: 'YYYY-MM'
    period: Mapped[str] = mapped_column(sa.Text, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(postgresql.JSONB, nullable=False)
    pdf_url: Mapped[str | None] = mapped_column(sa.Text)
    generated_at: Mapped[datetime.datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )
