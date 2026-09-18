"""Course catalogue and lesson listing (PRD section 10).

Read-only, and behind the token: nothing here is public. The catalogue is not
secret, but an unauthenticated endpoint is one more surface to keep correct for
no benefit — a learner has a token from the moment they open the Mini App.

**Organisation-scoped courses are invisible here.** ``courses.org_id`` is set
for the B-side job packs (PRD section 12), membership lands in M4 (BACKLOG G1),
and until something can check it the safe answer is that these endpoints serve
the consumer catalogue only. Filtering rather than omitting the filter is the
difference between "not built yet" and "leaks by default".
"""

from __future__ import annotations

import uuid
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.api.deps import CurrentUserDep, SessionDep
from app.core.errors import ContentNotFound
from app.models.content import Course, Lesson

router = APIRouter(prefix="/courses", tags=["learning"])


class CourseOut(BaseModel):
    id: uuid.UUID
    course_type: str
    language: str
    level: str
    #: Khmer is the teaching language and is never null; Chinese is a gloss.
    title_km: str
    title_zh: str | None


class LessonSummaryOut(BaseModel):
    id: uuid.UUID
    sequence: int
    title_km: str
    #: What this lesson teaches. Mastery is tracked per concept, not per
    #: lesson (PRD 3.2), so a client showing progress needs these.
    concept_ids: list[uuid.UUID]


@router.get("")
async def list_courses(
    session: SessionDep,
    user_id: CurrentUserDep,
    language: str | None = None,
    course_type: Annotated[str | None, Query(alias="type")] = None,
) -> list[CourseOut]:
    """The consumer catalogue, optionally narrowed.

    ``course_type`` is exposed as ``?type=`` to match PRD section 10; the
    parameter is renamed here only because ``type`` shadows a builtin.
    """
    statement = (
        sa.select(Course)
        .where(Course.org_id.is_(None))
        .order_by(Course.language, Course.level, Course.created_at)
    )
    if language is not None:
        statement = statement.where(Course.language == language)
    if course_type is not None:
        statement = statement.where(Course.course_type == course_type)

    rows = (await session.execute(statement)).scalars().all()
    return [
        CourseOut(
            id=row.id,
            course_type=row.course_type,
            language=row.language,
            level=row.level,
            title_km=row.title_km,
            title_zh=row.title_zh,
        )
        for row in rows
    ]


@router.get("/{course_id}/lessons")
async def list_lessons(
    course_id: uuid.UUID, session: SessionDep, user_id: CurrentUserDep
) -> list[LessonSummaryOut]:
    """One course's lessons in teaching order.

    Raises:
        ContentNotFound: no such course, or one this endpoint does not serve.
            An empty list would be a different claim — that the course exists
            and has no lessons — and a client cannot act on the difference
            unless we make it.
    """
    exists = await session.execute(
        sa.select(Course.id).where(Course.id == course_id, Course.org_id.is_(None))
    )
    if exists.scalar_one_or_none() is None:
        raise ContentNotFound(course_id=str(course_id))

    statement = sa.select(Lesson).where(Lesson.course_id == course_id).order_by(Lesson.sequence)
    rows = (await session.execute(statement)).scalars().all()
    return [
        LessonSummaryOut(
            id=row.id,
            sequence=row.sequence,
            title_km=row.title_km,
            concept_ids=list(row.concept_ids),
        )
        for row in rows
    ]
