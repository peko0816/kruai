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
from app.models.learning import LessonProgress

router = APIRouter(prefix="/courses", tags=["learning"])

#: A lesson with no progress row. Not a stored value — lesson_progress only has
#: 'started' and 'completed' (DATA_MODEL.sql) — so it is named here rather than
#: left as a null for each client to interpret.
NOT_STARTED = "not_started"


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
    #: not_started / started / completed, for this learner.
    status: str


class CourseLessonsOut(BaseModel):
    """A course's lessons, and which one this learner should do now.

    ``next_lesson_id`` is the server's answer, not a hint. Letting a client
    work it out is how the bot ended up offering lesson one forever: it took
    the first of the list, and the list does not know who is asking
    (docs/DECISIONS.md D-057).
    """

    lessons: list[LessonSummaryOut]
    #: None when every lesson in the course is finished.
    next_lesson_id: uuid.UUID | None


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
) -> CourseLessonsOut:
    """One course's lessons in teaching order, with this learner's progress.

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

    statement = (
        sa.select(Lesson, LessonProgress.status)
        .outerjoin(
            LessonProgress,
            sa.and_(
                LessonProgress.lesson_id == Lesson.id,
                LessonProgress.user_id == user_id,
            ),
        )
        .where(Lesson.course_id == course_id)
        .order_by(Lesson.sequence)
    )
    lessons = [
        LessonSummaryOut(
            id=row.id,
            sequence=row.sequence,
            title_km=row.title_km,
            concept_ids=list(row.concept_ids),
            status=status or NOT_STARTED,
        )
        for row, status in (await session.execute(statement)).all()
    ]
    return CourseLessonsOut(lessons=lessons, next_lesson_id=_next_lesson(lessons))


def _next_lesson(lessons: list[LessonSummaryOut]) -> uuid.UUID | None:
    """The first lesson this learner has not finished, in teaching order.

    Resuming outranks starting something new: a lesson left half done is the
    one they were in the middle of. Beyond that it is simply the next unfinished
    one, which is what "next" means in a course with a sequence.

    None when everything is done, which a client shows as "you are up to date"
    rather than by starting the first lesson again.
    """
    for lesson in lessons:
        if lesson.status == "started":
            return lesson.id
    for lesson in lessons:
        if lesson.status == NOT_STARTED:
            return lesson.id
    return None
