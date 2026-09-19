"""GET /api/v1/review/queue — what to practise now, weakest first.

The ordering and the slicing are services/mastery/review.py's, pure and
unit-tested. This module's job is to turn concept_mastery rows into candidates,
hand them over with the learner's chosen slot, and put the concepts back
together with enough content for a client to actually run the review.

Only concepts the schedule has released appear. A queue sorted by mastery alone
would surface a concept attempted five minutes ago — its score is still low —
and that is spaced repetition switched off (PRD 3.2).

The per-concept time estimate is a flat figure from configuration today. The
domain module takes it as an input precisely so that it can become a real
estimate — drill and vocab item counts live in lesson_items — by changing this
file rather than the ordering logic.
"""

from __future__ import annotations

import datetime
import uuid
from enum import IntEnum
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUserDep, SessionDep, SettingsDep
from app.core.logging import get_logger
from app.models.content import Concept
from app.models.learning import ConceptMastery
from app.services.mastery import ReviewCandidate, build_review_queue

log = get_logger(__name__)

router = APIRouter(prefix="/review", tags=["learning"])


class ReviewMinutes(IntEnum):
    """The slot lengths the interface offers (BACKLOG C3).

    An enum rather than a plain int so FastAPI validates the parameter itself
    and answers 422, instead of letting a value the interface never offered
    reach build_review_queue and raise there.

    Not ``Literal[5, 10, 15, 25]``, which looks equivalent and is not: a query
    string arrives as ``"10"`` and pydantic matches literals without coercing,
    so every request would be refused — including the ones using the offered
    values. The default worked, because a default is already an int, which is
    exactly the shape of bug that reaches production.

    It restates REVIEW_DURATION_CHOICES because neither form can be built from
    a tuple at runtime; a test below asserts the two agree.
    """

    FIVE = 5
    TEN = 10
    FIFTEEN = 15
    TWENTY_FIVE = 25


#: Slot length when the client does not choose one. The middle of what the
#: interface offers, and deliberately not user_profiles.daily_goal_minutes:
#: that column is a daily goal and accepts values the queue does not offer, so
#: reading it here would turn a profile edit into a refusal on this endpoint.
DEFAULT_REVIEW_MINUTES = ReviewMinutes.TEN


class ReviewEntryOut(BaseModel):
    """One concept to practise, with what the client needs to teach it."""

    concept_id: uuid.UUID
    #: Stable identifier from the content pack, e.g. zh.hsk1.want_noun.
    slug: str
    pattern: str
    km_explanation: str
    level: str
    mastery: float
    attempt_count: int
    #: None for a concept that has a row but was never scheduled — treated as
    #: due rather than left invisible.
    next_due_at: datetime.datetime | None
    estimated_seconds: int


class ReviewQueueOut(BaseModel):
    entries: list[ReviewEntryOut]
    requested_minutes: int
    estimated_seconds: int
    #: Due concepts that did not fit the slot. Lets a client say "12 more
    #: waiting" rather than implying the learner is finished.
    skipped: int
    #: True when one concept alone was longer than the whole slot. It is
    #: returned anyway (docs/DECISIONS.md D-017).
    over_budget: bool


@router.get("/queue")
async def get_review_queue(
    session: SessionDep,
    settings: SettingsDep,
    user_id: CurrentUserDep,
    minutes: Annotated[ReviewMinutes, Query(description="Slot length in minutes")] = (
        DEFAULT_REVIEW_MINUTES
    ),
) -> ReviewQueueOut:
    """The learner's due concepts, weakest and most overdue first."""
    now = datetime.datetime.now(datetime.UTC)
    rows = await _load_candidates(session, user_id=user_id)
    estimate = settings.review_estimated_seconds_per_concept

    queue = build_review_queue(
        [
            ReviewCandidate(
                concept_id=row.concept_id,
                mastery=row.mastery_score,
                next_due_at=row.next_due_at,
                estimated_seconds=estimate,
            )
            for row, _ in rows
        ],
        minutes=int(minutes),
        now=now,
    )

    concepts = {row.concept_id: concept for row, concept in rows}
    mastery_rows = {row.concept_id: row for row, _ in rows}

    log.info(
        "review.queue_served",
        user_id=str(user_id),
        minutes=int(minutes),
        due=len(queue.entries) + queue.skipped,
        served=len(queue.entries),
        skipped=queue.skipped,
    )

    return ReviewQueueOut(
        entries=[
            ReviewEntryOut(
                concept_id=entry.concept_id,
                slug=concepts[entry.concept_id].slug,
                pattern=concepts[entry.concept_id].pattern,
                km_explanation=concepts[entry.concept_id].km_explanation,
                level=concepts[entry.concept_id].level,
                mastery=entry.mastery,
                attempt_count=mastery_rows[entry.concept_id].attempt_count,
                next_due_at=entry.next_due_at,
                estimated_seconds=entry.estimated_seconds,
            )
            for entry in queue.entries
        ],
        requested_minutes=queue.requested_minutes,
        estimated_seconds=queue.estimated_seconds,
        skipped=queue.skipped,
        over_budget=queue.over_budget,
    )


async def _load_candidates(
    session: AsyncSession, *, user_id: uuid.UUID
) -> list[tuple[ConceptMastery, Concept]]:
    """This learner's tracked concepts, joined to the content that teaches them.

    The join cannot drop a row: concept_mastery.concept_id is a foreign key
    with no ON DELETE, so PostgreSQL refuses to remove a concept anyone has
    practised. (That was worth checking rather than assuming — an earlier
    version of this docstring claimed the inner join was defending against
    exactly that, and the test written to prove it could not even set the
    state up.) It is here to carry the pattern and the Khmer explanation, which
    a client needs to run the review at all.

    Everything is loaded and the slicing happens in the domain layer. A SQL
    LIMIT would have to reimplement the ordering rules, and then there would be
    two of them — one tested, one not.
    """
    statement = (
        sa.select(ConceptMastery, Concept)
        .join(Concept, Concept.id == ConceptMastery.concept_id)
        .where(ConceptMastery.user_id == user_id)
    )
    return [(row[0], row[1]) for row in (await session.execute(statement)).all()]


__all__ = ["router"]
