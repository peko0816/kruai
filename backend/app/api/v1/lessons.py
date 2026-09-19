"""GET /api/v1/lessons/{id} — a lesson with its media already chosen.

The response carries both URLs and an instruction. ARCHITECTURE 2.2 is explicit
that the choice is the server's: the client plays ``primary_url`` and switches
to ``fallback_url`` if it stalls, and it never inspects plan, data saver or item
type to decide. That is not a division of labour for its own sake — PRD 7.2
decides whether S3 happens by comparing completion between the two arms, and an
arm that each platform's client computes for itself is not an arm.

So this handler assembles the viewer once (plan, data saver, experiment arm) and
hands it to ``media.resolve`` per item, which is pure and fully unit-tested. The
handler itself decides nothing.

The experiment assignment happens here because this is where a learner is
actually exposed to the difference. While EXPERIMENT_EXPLAIN_MEDIA_ENABLED is
false — its default until S2 — ``assign`` reads nothing and writes nothing
(D-022), so this costs a function call.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, status
from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import CurrentUserDep, SessionDep, SessionFactoryDep, SettingsDep
from app.core.config import Settings
from app.core.errors import ContentNotFound, LessonNotStarted
from app.core.logging import get_logger
from app.models.content import Course, Lesson, LessonItem, MediaAsset
from app.models.learning import ConceptMastery, LessonProgress
from app.models.users import UserProfile
from app.services.entitlements import Entitlements, QuotaReset, current_plan
from app.services.experiments import EXPLAIN_MEDIA, Experiments
from app.services.mastery import INITIAL_INTERVAL_DAYS, initial_ease_factor
from app.services.media import MediaCandidate, ResolvedMedia, ViewerContext, resolve

log = get_logger(__name__)

router = APIRouter(prefix="/lessons", tags=["learning"])


class MediaOut(BaseModel):
    """What to play, what to fall back to, and why it came out that way.

    ``audio_url`` and ``video_url`` are facts about the content and are always
    reported when they exist; ``primary_url`` is the instruction. ``decision``
    is the learner's own answer to "why am I seeing this" — it is what makes a
    support question answerable without reconstructing their account state.
    """

    primary_kind: str | None
    primary_url: str | None
    fallback_url: str | None
    audio_url: str | None
    video_url: str | None
    decision: str
    #: How long to wait before switching to the fallback (PRD 7.3). Sent so the
    #: threshold can move without shipping a client.
    client_timeout_ms: int
    #: i18n key for the AI disclaimer, set only when video is served (PRD 7.4).
    disclaimer_key: str | None


class LessonItemOut(BaseModel):
    id: uuid.UUID
    sequence: int
    item_type: str
    concept_id: uuid.UUID | None
    #: The content pack's own JSON for this item, passed through unchanged.
    payload: dict[str, Any]
    media: MediaOut


class LessonDetailOut(BaseModel):
    id: uuid.UUID
    course_id: uuid.UUID
    sequence: int
    title_km: str
    concept_ids: list[uuid.UUID]
    items: list[LessonItemOut]


class LessonStartOut(BaseModel):
    """The learner has begun this lesson, and it cost them a task."""

    lesson_id: uuid.UUID
    status: str
    #: False when they had already started it — resuming is free.
    first_start: bool
    #: Tasks left today; None when the plan has no daily cap.
    remaining_tasks: int | None


class LessonCompletionOut(BaseModel):
    """What completing a lesson left behind."""

    lesson_id: uuid.UUID
    status: str
    completed_at: datetime.datetime
    #: Concepts of this lesson that now have a mastery row and a due date.
    concepts_tracked: int
    #: Concepts this call put on the review schedule for the first time —
    #: the ones the learner never spoke to.
    concepts_newly_scheduled: int
    #: False when the lesson had already been completed before this call.
    first_completion: bool


@router.get("/{lesson_id}")
async def get_lesson(
    lesson_id: uuid.UUID,
    session: SessionDep,
    session_factory: SessionFactoryDep,
    settings: SettingsDep,
    user_id: CurrentUserDep,
) -> LessonDetailOut:
    """One lesson, its items in order, each with its media already resolved.

    Raises:
        ContentNotFound: no such lesson, or one belonging to a course this
            endpoint does not serve (see courses.py on org scoping).
        LessonNotStarted: the learner has not opened this lesson. The contents
            are the lesson, so handing them over without a start would make the
            daily task allowance a thing clients enforce on themselves — which
            is precisely what ARCHITECTURE section 1 forbids.
    """
    lesson = await _load_lesson(session, lesson_id)
    await _require_started(session, user_id=user_id, lesson_id=lesson_id)
    viewer = await _load_viewer(
        session, session_factory=session_factory, settings=settings, user_id=user_id
    )
    items = await _load_items(session, lesson_id)

    resolved = [(item, resolve(candidate, viewer, settings=settings)) for item, candidate in items]

    log.info(
        "lesson.served",
        user_id=str(user_id),
        lesson_id=str(lesson_id),
        plan=viewer.plan,
        data_saver=viewer.data_saver,
        experiment_variant=viewer.experiment_variant,
        item_count=len(resolved),
        # Counted rather than listed: one line per lesson, and a shift in the
        # mix is the thing worth noticing.
        decisions=sorted({media.decision for _, media in resolved}),
    )

    return LessonDetailOut(
        id=lesson.id,
        course_id=lesson.course_id,
        sequence=lesson.sequence,
        title_km=lesson.title_km,
        concept_ids=list(lesson.concept_ids),
        items=[_item_out(item, media) for item, media in resolved],
    )


async def _load_lesson(session: AsyncSession, lesson_id: uuid.UUID) -> Lesson:
    """The lesson, provided its course is one this endpoint serves.

    Joined to courses rather than fetched alone: a lesson inherits its
    visibility from its course, and checking it here means a leaked lesson id
    is not a way around the catalogue's org filter.
    """
    statement = (
        sa.select(Lesson)
        .join(Course, Course.id == Lesson.course_id)
        .where(Lesson.id == lesson_id, Course.org_id.is_(None))
    )
    lesson = (await session.execute(statement)).scalar_one_or_none()
    if lesson is None:
        raise ContentNotFound(lesson_id=str(lesson_id))
    return lesson


async def _load_viewer(
    session: AsyncSession,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    user_id: uuid.UUID,
) -> ViewerContext:
    """The three things media selection needs to know about this learner.

    ``data_saver`` falls back to MEDIA_DEFAULT_DATA_SAVER if the profile row is
    somehow missing. Auth provisions one at first sign-in (D-029), so this is
    defence rather than a path — but the alternative is a 500 on a lesson read,
    and the conservative answer for a learner we know nothing about is the
    cheaper stream.
    """
    data_saver = (
        await session.execute(
            sa.select(UserProfile.data_saver).where(UserProfile.user_id == user_id)
        )
    ).scalar_one_or_none()
    plan = await current_plan(session, user_id)

    experiments = Experiments(session_factory=session_factory, settings=settings)
    variant = await experiments.assign(user_id, experiment_key=EXPLAIN_MEDIA.key)

    return ViewerContext(
        plan=plan,
        data_saver=settings.media_default_data_saver if data_saver is None else data_saver,
        experiment_variant=variant,
    )


async def _load_items(
    session: AsyncSession, lesson_id: uuid.UUID
) -> list[tuple[LessonItem, MediaCandidate]]:
    """Items in teaching order, each paired with whatever assets it has.

    One outer join rather than a query per item: a lesson is a handful of items
    and each has at most two assets, and the N+1 version would be N+1 round
    trips for a page every learner loads.
    """
    statement = (
        sa.select(LessonItem, MediaAsset.kind, MediaAsset.url)
        .outerjoin(MediaAsset, MediaAsset.lesson_item_id == LessonItem.id)
        .where(LessonItem.lesson_id == lesson_id)
        .order_by(LessonItem.sequence)
    )

    items: dict[uuid.UUID, LessonItem] = {}
    assets: dict[uuid.UUID, dict[str, str]] = {}
    for item, kind, url in (await session.execute(statement)).all():
        items.setdefault(item.id, item)
        if kind is not None:
            assets.setdefault(item.id, {})[kind] = url

    return [
        (
            item,
            MediaCandidate(
                item_media_type=item.media_type,
                audio_url=assets.get(item.id, {}).get("audio"),
                video_url=assets.get(item.id, {}).get("video"),
            ),
        )
        for item in items.values()
    ]


def _item_out(item: LessonItem, media: ResolvedMedia) -> LessonItemOut:
    return LessonItemOut(
        id=item.id,
        sequence=item.sequence,
        item_type=item.item_type,
        concept_id=item.concept_id,
        payload=item.payload,
        media=MediaOut(
            primary_kind=media.primary_kind,
            primary_url=media.primary_url,
            fallback_url=media.fallback_url,
            audio_url=media.audio_url,
            video_url=media.video_url,
            decision=media.decision,
            client_timeout_ms=media.client_timeout_ms,
            disclaimer_key=media.disclaimer_key,
        ),
    )


# ---------------------------------------------------------------- completion


@router.post("/{lesson_id}/complete", status_code=status.HTTP_200_OK)
async def complete_lesson(
    lesson_id: uuid.UUID,
    session: SessionDep,
    settings: SettingsDep,
    user_id: CurrentUserDep,
) -> LessonCompletionOut:
    """Mark a lesson finished and make sure all of its concepts are scheduled.

    PRD 3.3 ends a lesson with "update concept_mastery, write the review
    queue". The mastery half already happened: D3 moves a concept's score on
    every attempt. What is left is the concepts the learner never spoke to —
    a lesson they clicked through, or items they skipped. Those have no
    concept_mastery row at all, so spaced repetition would never surface them
    and the lesson would be "done" while part of it had never been practised.

    So completion seeds a row for every concept in the lesson that lacks one,
    due tomorrow. Concepts that already have a row are left exactly as they
    are: their schedule is in flight and the stored row outranks any recompute
    (the same rule as docs/DECISIONS.md D-023).

    Idempotent. Completing twice keeps the first ``completed_at`` — a learner
    revisiting a lesson has not un-finished it, and moving the timestamp would
    quietly rewrite when they got there.

    Raises:
        ContentNotFound: no such lesson, or one behind an organisation course.
        LessonNotStarted: nobody opened it. Completing a lesson that was never
            started would write progress for work the allowance never covered.
    """
    now = datetime.datetime.now(datetime.UTC)
    lesson = await _load_lesson(session, lesson_id)
    await _require_started(session, user_id=user_id, lesson_id=lesson_id)

    completed_at, first_completion = await _mark_completed(
        session, user_id=user_id, lesson_id=lesson_id, now=now
    )
    newly_scheduled = await _schedule_untouched_concepts(
        session,
        user_id=user_id,
        concept_ids=list(lesson.concept_ids),
        now=now,
        settings=settings,
    )
    await session.commit()

    log.info(
        "lesson.completed",
        user_id=str(user_id),
        lesson_id=str(lesson_id),
        first_completion=first_completion,
        concepts=len(lesson.concept_ids),
        newly_scheduled=newly_scheduled,
    )

    return LessonCompletionOut(
        lesson_id=lesson_id,
        status="completed",
        completed_at=completed_at,
        concepts_tracked=len(lesson.concept_ids),
        concepts_newly_scheduled=newly_scheduled,
        first_completion=first_completion,
    )


async def _mark_completed(
    session: AsyncSession, *, user_id: uuid.UUID, lesson_id: uuid.UUID, now: datetime.datetime
) -> tuple[datetime.datetime, bool]:
    """Write lesson_progress, keeping the first completion time.

    The upsert only promotes a row that is not yet completed, so a second call
    changes nothing and the returned timestamp is whatever the first one wrote.
    """
    statement = (
        pg_insert(LessonProgress)
        .values(user_id=user_id, lesson_id=lesson_id, status="completed", completed_at=now)
        .on_conflict_do_update(
            index_elements=["user_id", "lesson_id"],
            set_={"status": "completed", "completed_at": now},
            where=LessonProgress.status != "completed",
        )
        .returning(LessonProgress.completed_at)
    )
    written: datetime.datetime | None = (await session.execute(statement)).scalar_one_or_none()
    if written is not None:
        return written, True

    # The upsert's WHERE refused, which means a completed row is already there.
    stored = await session.execute(
        sa.select(LessonProgress.completed_at).where(
            LessonProgress.user_id == user_id, LessonProgress.lesson_id == lesson_id
        )
    )
    previous = stored.scalar_one()
    # completed_at is nullable in the DDL; a completed row without one would
    # mean something else wrote it, and there is no earlier time to preserve.
    return previous or now, False


async def _schedule_untouched_concepts(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    concept_ids: list[uuid.UUID],
    now: datetime.datetime,
    settings: Settings,
) -> int:
    """Give every unpractised concept of this lesson a row and a due date.

    ``ON CONFLICT DO NOTHING`` is the whole guarantee: a concept the learner
    has attempted keeps its mastery, its ease and its next_due_at untouched.

    ease_factor comes from SM2_EASE_INITIAL rather than the column default,
    for the reason in services/mastery/sm2.py (the retired constraint L-6):
    the two agree today and would stop agreeing silently.
    """
    if not concept_ids:
        return 0

    statement = (
        pg_insert(ConceptMastery)
        .values(
            [
                {
                    "user_id": user_id,
                    "concept_id": concept_id,
                    "mastery_score": 0.0,
                    "attempt_count": 0,
                    "ease_factor": initial_ease_factor(settings),
                    "interval_days": INITIAL_INTERVAL_DAYS,
                    "next_due_at": now + datetime.timedelta(days=INITIAL_INTERVAL_DAYS),
                }
                for concept_id in concept_ids
            ]
        )
        .on_conflict_do_nothing(index_elements=["user_id", "concept_id"])
        .returning(ConceptMastery.concept_id)
    )
    return len((await session.execute(statement)).scalars().all())


# ------------------------------------------------------------------- starting


@router.post("/{lesson_id}/start", status_code=status.HTTP_200_OK)
async def start_lesson(
    lesson_id: uuid.UUID,
    session: SessionDep,
    session_factory: SessionFactoryDep,
    settings: SettingsDep,
    user_id: CurrentUserDep,
) -> LessonStartOut:
    """Begin a lesson, spending one of the day's tasks.

    **This is where the Free tier's task allowance is enforced** (PRD 4.3, three
    a day), and it is enforced here rather than at completion for one reason: a
    learner refused after finishing a lesson has already done the work, so the
    refusal protects nothing and costs them everything. A paywall has to be a
    door, not a bill.

    What counts as a task is one completed lesson (docs/DECISIONS.md D-043), so
    the allowance is spent on the *first* start of each lesson. Picking one back
    up is free: a learner who does one lesson over three sittings has done one
    lesson, and charging them three times would make the limit mean something
    nobody agreed to.

    Raises:
        ContentNotFound: no such lesson, or one behind an organisation course.
        InsufficientQuota: the day's tasks are spent (402).
    """
    now = datetime.datetime.now(datetime.UTC)
    await _load_lesson(session, lesson_id)
    plan = await current_plan(session, user_id)
    timezone = await _timezone_of(session, user_id)

    # Every read first, then the connection goes back, then the day's counters
    # are rolled over on a session of their own. Only after that does this
    # request touch the database again -- one connection at a time, or a
    # poolful of starts deadlock each other (D-073).
    await session.commit()
    if timezone is not None:
        reset = QuotaReset(session_factory=session_factory, settings=settings)
        await reset.reset_if_due(user_id, timezone=timezone, now=now)

    started = await _mark_started(session, user_id=user_id, lesson_id=lesson_id)
    if not started:
        await session.commit()
        return LessonStartOut(
            lesson_id=lesson_id,
            status="started",
            first_start=False,
            remaining_tasks=None,
        )

    entitlements = Entitlements(session_factory=session_factory, settings=settings)
    try:
        # On this session, so the row that marks the lesson started and the
        # allowance that paid for it are one transaction.
        consumption = await entitlements.consume_task(user_id, plan=plan, session=session)
    except Exception:
        # The row was inserted in this transaction and the allowance said no, so
        # the start never happened. Rolling back is what keeps a refused learner
        # from finding the lesson already marked started when they come back.
        await session.rollback()
        raise

    await session.commit()
    log.info(
        "lesson.started",
        user_id=str(user_id),
        lesson_id=str(lesson_id),
        plan=plan,
        remaining_tasks=consumption.remaining,
    )
    return LessonStartOut(
        lesson_id=lesson_id,
        status="started",
        first_start=True,
        remaining_tasks=consumption.remaining,
    )


async def _mark_started(session: AsyncSession, *, user_id: uuid.UUID, lesson_id: uuid.UUID) -> bool:
    """Record the start. True when this call was the first one.

    DO NOTHING rather than DO UPDATE: a lesson already started — or already
    completed — must not be reopened, because either would charge a second task
    for work the learner has already paid for.
    """
    statement = (
        pg_insert(LessonProgress)
        .values(user_id=user_id, lesson_id=lesson_id, status="started")
        .on_conflict_do_nothing(index_elements=["user_id", "lesson_id"])
        .returning(LessonProgress.lesson_id)
    )
    return (await session.execute(statement)).scalar_one_or_none() is not None


async def _timezone_of(session: AsyncSession, user_id: uuid.UUID) -> str | None:
    stored = await session.execute(
        sa.select(UserProfile.timezone).where(UserProfile.user_id == user_id)
    )
    timezone: str | None = stored.scalar_one_or_none()
    return timezone


async def _require_started(
    session: AsyncSession, *, user_id: uuid.UUID, lesson_id: uuid.UUID
) -> None:
    """Refuse a learner who never opened this lesson.

    The point is not bookkeeping. Starting is what spends a task (D-054), and
    before this check a learner with no tasks left could be refused at the door
    and then read the whole lesson and mark it complete anyway — the allowance
    held only for clients that volunteered to ask. Found by probing the built
    endpoints rather than by a test, which is why there are now tests.

    Raises:
        LessonNotStarted: no lesson_progress row.
    """
    stored = await session.execute(
        sa.select(LessonProgress.status).where(
            LessonProgress.user_id == user_id, LessonProgress.lesson_id == lesson_id
        )
    )
    if stored.scalar_one_or_none() is None:
        raise LessonNotStarted(lesson_id=str(lesson_id))
