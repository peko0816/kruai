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

import uuid
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import CurrentUserDep, SessionDep, SessionFactoryDep, SettingsDep
from app.core.config import Settings
from app.core.errors import ContentNotFound
from app.core.logging import get_logger
from app.models.content import Course, Lesson, LessonItem, MediaAsset
from app.models.users import UserProfile
from app.services.entitlements import current_plan
from app.services.experiments import EXPLAIN_MEDIA, Experiments
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
    """
    lesson = await _load_lesson(session, lesson_id)
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
