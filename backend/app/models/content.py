"""Courses, concepts, lessons, media. Mirrors docs/DATA_MODEL.sql section 内容."""

from __future__ import annotations

import datetime
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Course(Base):
    """Both entries share this table; only course_type differs (PRD section 2)."""

    __tablename__ = "courses"
    __table_args__ = (
        sa.CheckConstraint("course_type IN ('exam','job')", name="courses_course_type_check"),
        sa.Index("idx_courses_lookup", "language", "course_type", "level"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )
    course_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    language: Mapped[str] = mapped_column(sa.Text, nullable=False)
    level: Mapped[str] = mapped_column(sa.Text, nullable=False)
    title_km: Mapped[str] = mapped_column(sa.Text, nullable=False)
    title_zh: Mapped[str | None] = mapped_column(sa.Text)
    #: Set for course_type='job'. The DDL adds this FK last, after orgs exists.
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("orgs.id", ondelete="CASCADE", name="fk_courses_org"),
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )


class Concept(Base):
    """The unit of mastery. The system tracks concepts, not lessons (PRD 3.2)."""

    __tablename__ = "concepts"
    __table_args__ = (sa.Index("idx_concepts_level", "language", "level", "sort_order"),)

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )
    #: e.g. zh.hsk1.want_noun
    slug: Mapped[str] = mapped_column(sa.Text, unique=True, nullable=False)
    language: Mapped[str] = mapped_column(sa.Text, nullable=False)
    level: Mapped[str] = mapped_column(sa.Text, nullable=False)
    pattern: Mapped[str] = mapped_column(sa.Text, nullable=False)
    km_explanation: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: Typical Khmer-L1 errors; grows from human input and attempt data.
    common_l1_errors: Mapped[list[Any]] = mapped_column(
        postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
    )
    #: HSKK speaking task types. validate.py rejects an empty value (PRD 6.2).
    hskk_task_types: Mapped[list[str]] = mapped_column(
        postgresql.ARRAY(sa.Text), nullable=False, server_default=sa.text("'{}'")
    )
    sort_order: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("0"))


class Lesson(Base):
    __tablename__ = "lessons"
    __table_args__ = (sa.UniqueConstraint("course_id", "sequence"),)

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )
    course_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("courses.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: PostgreSQL cannot foreign-key array elements, so this is unenforced by
    #: design; import_pack.py is responsible for referential integrity here.
    concept_ids: Mapped[list[uuid.UUID]] = mapped_column(
        postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=False
    )
    sequence: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    title_km: Mapped[str] = mapped_column(sa.Text, nullable=False)


class LessonItem(Base):
    __tablename__ = "lesson_items"
    __table_args__ = (
        sa.CheckConstraint(
            "item_type IN ('explain','drill','vocab','qa')",
            name="lesson_items_item_type_check",
        ),
        sa.CheckConstraint("media_type IN ('audio','video')", name="lesson_items_media_type_check"),
        sa.UniqueConstraint("lesson_id", "sequence"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )
    lesson_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("lessons.id", ondelete="CASCADE"),
        nullable=False,
    )
    concept_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), sa.ForeignKey("concepts.id")
    )
    item_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(postgresql.JSONB, nullable=False)
    media_type: Mapped[str] = mapped_column(
        sa.Text, nullable=False, server_default=sa.text("'audio'")
    )
    #: Only anchor items are candidates for S2 video generation (PRD 7.2).
    anchor: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("false")
    )
    sequence: Mapped[int] = mapped_column(sa.Integer, nullable=False)


class MediaAsset(Base):
    """One item can carry an audio row and a video row at the same time.

    The runtime picks between them by plan, data_saver and experiment variant
    (PRD 7.3); the client only plays the primary URL and falls back.
    """

    __tablename__ = "media_assets"
    __table_args__ = (
        sa.CheckConstraint("kind IN ('audio','video')", name="media_assets_kind_check"),
        sa.UniqueConstraint("lesson_item_id", "kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )
    lesson_item_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("lesson_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    url: Mapped[str] = mapped_column(sa.Text, nullable=False)
    duration_ms: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("0")
    )
    bytes: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("0"))
    provider: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: Changing the source text expires the asset and forces regeneration.
    source_text_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    generated_at: Mapped[datetime.datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )


class ContentPack(Base):
    """Frozen content. sources/licence are the copyright boundary (PRD 5.5)."""

    __tablename__ = "content_packs"
    __table_args__ = (
        sa.UniqueConstraint("language", "level", "version"),
        sa.CheckConstraint("jsonb_array_length(sources) > 0", name="sources_not_empty"),
        sa.CheckConstraint("length(trim(licence)) > 0", name="licence_not_blank"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )
    version: Mapped[str] = mapped_column(sa.Text, nullable=False)
    language: Mapped[str] = mapped_column(sa.Text, nullable=False)
    level: Mapped[str] = mapped_column(sa.Text, nullable=False)
    checksum: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: Non-empty is enforced in the database, not only in import_pack.py.
    sources: Mapped[list[Any]] = mapped_column(postgresql.JSONB, nullable=False)
    licence: Mapped[str] = mapped_column(sa.Text, nullable=False)
    published_at: Mapped[datetime.datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
    )
