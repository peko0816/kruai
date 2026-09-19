"""POST /api/v1/attempts — one spoken attempt, end to end (ARCHITECTURE 2.1).

    quota -> scoring -> cost_ledger -> attempts row -> mastery -> schedule

Everything after ``scoring`` is deliberately ignorant of who scored: the
provider is chosen by the registry, returns a value object, and M0-1's decision
lands as one new file rather than a change to this chain.

**Order, and why it is this order.** The allowance is spent *before* the
recording is sent anywhere. A learner who is out of attempts must not be able to
trigger a paid assessment — the daily cap is what holds PRD 11.2's per-user cost
ceiling, and a check that happened after the call would be decoration. But
ARCHITECTURE section 5 also says a provider failure must not cost the learner an
attempt, and there is no way to satisfy both with one atomic statement. So the
deduction is compensated: if the scorer comes back ok=False, the attempt is
released again (D-034). The window in between is the only thing that can lose a
learner an attempt, and it loses it in the direction that protects the budget.

**Nothing here decides anything.** Whether the attempt passed, how far mastery
moves, when the concept comes back — all of it is in services/mastery, pure and
unit-tested. This module's job is to fetch, to order, and to persist.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, File, Form, UploadFile
from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import CurrentUserDep, SessionDep, SessionFactoryDep, SettingsDep
from app.core.config import Plan, Settings
from app.core.errors import (
    AudioTooLarge,
    ContentNotFound,
    CostCapReached,
    ItemNotScorable,
    ScoringUnavailable,
)
from app.core.logging import get_logger
from app.models.content import Concept, Course, Lesson, LessonItem
from app.models.learning import Attempt, ConceptMastery
from app.models.users import UserProfile
from app.services.cost_ledger import CostLedger
from app.services.entitlements import (
    Entitlements,
    QuotaReset,
    alert_threshold_usd_cents,
    current_plan,
    is_over_threshold,
    monthly_spend_usd_cents,
)
from app.services.mastery import (
    INITIAL_INTERVAL_DAYS,
    MasteryUpdate,
    ReviewSchedule,
    apply_attempt,
    initial_ease_factor,
    is_passing,
    schedule_review,
)
from app.services.scoring.base import AssessMode, Language, PronunciationResult
from app.services.scoring.registry import get_scorer

log = get_logger(__name__)

router = APIRouter(prefix="/attempts", tags=["learning"])

#: courses.language holds a bare tag; the scorer speaks BCP-47. One mapping,
#: here, rather than a locale string stored twice in the content pack.
_LANGUAGES: dict[str, Language] = {"zh": Language.ZH_CN, "en": Language.EN_US}

#: Where the target sentence lives in lesson_items.payload. The pack schema is
#: E1's to define; this is the field D3 reads and E4 must validate (D-036).
#:
#: Named ``_FIELD`` rather than ``_KEY``: a name ending in _KEY declares that it
#: holds an i18n message key (bot/i18n_check.py enforces that they exist), and
#: this is a JSONB field name. Two kinds of "key" that look identical is exactly
#: what that convention is there to separate.
TARGET_TEXT_FIELD = "target_text"

#: Item types a learner can speak to. 'explain' is a lecture card.
SCORABLE_ITEM_TYPES = frozenset({"drill", "vocab", "qa"})

#: Items with a known reference sentence are scored against it; an open answer
#: has nothing to align to (scoring/base.py AssessMode).
SCRIPTED_ITEM_TYPES = frozenset({"drill", "vocab"})

#: i18n keys for the spoken feedback (CLAUDE.md section 7 — no inline copy).
FEEDBACK_PASSED_KEY = "attempt.feedback.passed"
FEEDBACK_RETRY_KEY = "attempt.feedback.retry"


class PhonemeOut(BaseModel):
    phoneme: str
    accuracy: float
    offset_ms: int | None
    duration_ms: int | None


class WordScoreOut(BaseModel):
    """Per-word detail, with phonemes nested as the provider reports them.

    PRD section 10 sketches a flat ``phonemes[]``; keeping the word alignment
    is strictly more information and is what scoring/base.py actually returns.
    Flattening would throw away which word a bad phoneme belonged to, which is
    the only thing that makes it actionable for a learner.
    """

    word: str
    accuracy: float
    error_type: str | None
    phonemes: list[PhonemeOut]


class ScoresOut(BaseModel):
    """Dimensions the provider reported. None means not applicable.

    completeness is scripted-only and prosody is en-US only (PRD section 8), so
    a client must handle absence rather than treat it as zero.
    """

    pron: float | None
    accuracy: float | None
    fluency: float | None
    completeness: float | None
    prosody: float | None
    tone: float | None


class FeedbackOut(BaseModel):
    """What to tell the learner, as keys and content — never as a sentence.

    The Khmer wording is i18n's (BACKLOG D7) and the explanation is the content
    pack's. Composing a sentence here would put user-visible copy in Python,
    which CLAUDE.md section 7 forbids, and would put it in one language.
    """

    key: str
    #: concepts.km_explanation, when this item teaches a concept.
    km_explanation: str | None


class MasteryOut(BaseModel):
    concept_id: uuid.UUID
    previous: float
    current: float
    interval_days: int
    next_due_at: datetime.datetime


class AttemptOut(BaseModel):
    id: uuid.UUID
    passed: bool
    scores: ScoresOut
    words: list[WordScoreOut]
    feedback: FeedbackOut
    #: Absent when the item teaches no concept — there is nothing to advance.
    mastery: MasteryOut | None
    #: Attempts left today; None when the plan has no daily cap.
    remaining_attempts: int | None


def usable_score(result: PronunciationResult) -> float | None:
    """The composite score, if the provider stands behind it. None if not.

    Two conditions, not one, and they are not redundant. ``ok`` is the
    provider's own verdict and outranks everything: scoring/base.py promises a
    failure arrives as ok=False rather than as an exception, so a result that
    says it failed is a failure whatever else it carries. A missing
    ``pron_score`` is the second door, because mastery has nothing to apply
    without one.

    Today's fake sets both together, so either check alone would look
    sufficient — until a vendor returns ok=False alongside a partial score and
    the learner is charged for it, gets an attempt row, and has their mastery
    moved by a number the provider disowned. The D3 mutation that deleted the
    ``ok`` half survived the whole integration suite for exactly that reason.
    """
    if not result.ok:
        return None
    return result.pron_score


@dataclass(frozen=True)
class _Target:
    """The item being attempted, with everything the chain needs about it."""

    item: LessonItem
    language: Language
    reference_text: str | None
    mode: AssessMode
    km_explanation: str | None


@router.post("", status_code=201)
async def create_attempt(
    session: SessionDep,
    session_factory: SessionFactoryDep,
    settings: SettingsDep,
    user_id: CurrentUserDep,
    lesson_item_id: Annotated[uuid.UUID, Form()],
    audio: Annotated[UploadFile, File()],
) -> AttemptOut:
    """Score one recording and advance the learner's mastery.

    Raises:
        ContentNotFound: no such item, or one behind an organisation's course.
        ItemNotScorable: an attempt against a lecture card.
        AudioTooLarge: over ATTEMPT_MAX_AUDIO_BYTES.
        InsufficientQuota: the daily allowance is spent (402).
        CostCapReached: this learner has cost more this month than their plan
            allows for (429).
        ScoringUnavailable: the provider could not assess it. Nothing is
            charged and nothing is stored.
    """
    now = datetime.datetime.now(datetime.UTC)
    target = await _load_target(session, lesson_item_id)
    payload = await _read_audio(audio, settings=settings)

    plan = await current_plan(session, user_id)
    await _guard_cost(session, user_id=user_id, plan=plan, settings=settings, now=now)
    entitlements = Entitlements(session_factory=session_factory, settings=settings)
    await _reset_quota_if_due(
        session, session_factory=session_factory, settings=settings, user_id=user_id, now=now
    )
    consumption = await entitlements.consume_attempt(user_id, plan=plan)

    result = await _assess(
        payload,
        target=target,
        user_id=user_id,
        session_factory=session_factory,
        settings=settings,
    )
    pron_score = usable_score(result)
    if pron_score is None:
        # ARCHITECTURE section 5: our failure, not the learner's. Give the
        # attempt back before refusing (D-034).
        await entitlements.release_attempt(user_id)
        log.warning(
            "attempt.scoring_failed",
            user_id=str(user_id),
            lesson_item_id=str(lesson_item_id),
            error_code=result.error_code,
        )
        raise ScoringUnavailable(reason=result.error_code or "no_score_returned")

    passed = is_passing(pron_score, settings=settings)
    attempt_id = await _record_attempt(
        session,
        user_id=user_id,
        target=target,
        result=result,
        passed=passed,
        provider=get_scorer(target.language, settings=settings).name,
    )
    mastery = await _advance_mastery(
        session,
        user_id=user_id,
        target=target,
        pron_score=pron_score,
        now=now,
        settings=settings,
    )
    await session.commit()

    log.info(
        "attempt.scored",
        user_id=str(user_id),
        attempt_id=str(attempt_id),
        lesson_item_id=str(lesson_item_id),
        concept_id=str(target.item.concept_id) if target.item.concept_id else None,
        pron_score=pron_score,
        passed=passed,
        cost_usd_cents=result.cost_usd_cents,
        mastery=None if mastery is None else mastery[0].current,
    )

    return _attempt_out(
        attempt_id,
        target=target,
        result=result,
        passed=passed,
        mastery=mastery,
        remaining_attempts=consumption.remaining,
    )


# ------------------------------------------------------------------ the pieces


async def _load_target(session: AsyncSession, lesson_item_id: uuid.UUID) -> _Target:
    """The item, its language, and the sentence it expects to hear.

    Joined through lessons and courses so an item inherits its course's
    visibility — the same rule the catalogue applies (D-031). ``concepts`` is
    joined in the same query because the Khmer explanation goes back with the
    score and a second round trip would buy nothing.
    """
    statement = (
        sa.select(LessonItem, Course.language, Concept.km_explanation)
        .join(Lesson, Lesson.id == LessonItem.lesson_id)
        .join(Course, Course.id == Lesson.course_id)
        .outerjoin(Concept, Concept.id == LessonItem.concept_id)
        .where(LessonItem.id == lesson_item_id, Course.org_id.is_(None))
    )
    row = (await session.execute(statement)).first()
    if row is None:
        raise ContentNotFound(lesson_item_id=str(lesson_item_id))

    item, language_tag, km_explanation = row
    if item.item_type not in SCORABLE_ITEM_TYPES:
        raise ItemNotScorable(lesson_item_id=str(lesson_item_id), item_type=item.item_type)

    language = _LANGUAGES.get(language_tag)
    if language is None:
        # An imported pack in a language no scorer knows about. The pipeline
        # should not have produced it; failing loudly beats scoring it as zh.
        raise ValueError(
            f"course language {language_tag!r} has no scorer mapping; known: {sorted(_LANGUAGES)}"
        )

    scripted = item.item_type in SCRIPTED_ITEM_TYPES
    return _Target(
        item=item,
        language=language,
        reference_text=_reference_text(item) if scripted else None,
        mode=AssessMode.SCRIPTED if scripted else AssessMode.UNSCRIPTED,
        km_explanation=km_explanation,
    )


def _reference_text(item: LessonItem) -> str:
    """The sentence a scripted item expects, out of the pack's payload.

    Raises:
        ValueError: the payload has no usable target. That is an invalid pack
            that got past import, and it is our defect rather than the
            learner's — so it fails loudly here instead of degrading into an
            unscripted assessment nobody asked for (D-036).
    """
    value = item.payload.get(TARGET_TEXT_FIELD)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"lesson_item {item.id} is {item.item_type!r} but its payload has no "
            f"usable {TARGET_TEXT_FIELD!r}; the pack should not have imported"
        )
    return value


async def _guard_cost(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    plan: Plan,
    settings: Settings,
    now: datetime.datetime,
) -> None:
    """Refuse before anything is spent, when this learner has cost too much.

    Checked ahead of the allowance deduction and ahead of the scorer, because
    both of those are things a throttled learner should not have happen: the
    first would take an attempt they never got to use, the second is the actual
    money (PRD 11.3).

    Raises:
        CostCapReached: past the plan's alert threshold for this month.
    """
    spend = await monthly_spend_usd_cents(session, user_id=user_id, now=now)
    if not is_over_threshold(spend, plan=plan, settings=settings):
        return

    threshold = alert_threshold_usd_cents(plan, settings=settings)
    # The alert half of PRD 11.3. Error rather than warning: a learner costing
    # half again what their plan allows is either a pricing problem or an
    # abuse, and both want somebody to look.
    log.error(
        "cost.cap_exceeded",
        user_id=str(user_id),
        plan=plan,
        spend_usd_cents=spend,
        threshold_usd_cents=threshold,
        cap_usd_cents=settings.monthly_cost_cap_usd_cents(plan),
    )
    raise CostCapReached(plan=plan, spend_usd_cents=spend, threshold_usd_cents=threshold)


async def _read_audio(audio: UploadFile, *, settings: Settings) -> bytes:
    """The uploaded bytes, refusing anything oversized.

    Read in bounded chunks rather than with ``.read()``: the whole point is not
    to hold an arbitrary upload in memory, and reading it all to measure it
    would do exactly that.
    """
    limit = settings.attempt_max_audio_bytes
    chunks: list[bytes] = []
    size = 0
    while chunk := await audio.read(64 * 1024):
        size += len(chunk)
        if size > limit:
            raise AudioTooLarge(limit_bytes=limit)
        chunks.append(chunk)
    return b"".join(chunks)


async def _reset_quota_if_due(
    session: AsyncSession,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    user_id: uuid.UUID,
    now: datetime.datetime,
) -> None:
    """Roll the daily counters over if the learner's local day has turned.

    Called on the way in rather than from a scheduled job: a reset that depends
    on a worker running is a reset that silently does not happen, and the
    guarded UPDATE in reset.py is already safe to call on every request.
    """
    timezone = (
        await session.execute(sa.select(UserProfile.timezone).where(UserProfile.user_id == user_id))
    ).scalar_one_or_none()
    if timezone is None:
        return
    reset = QuotaReset(session_factory=session_factory, settings=settings)
    await reset.reset_if_due(user_id, timezone=timezone, now=now)


async def _assess(
    payload: bytes,
    *,
    target: _Target,
    user_id: uuid.UUID,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> PronunciationResult:
    """Send the recording to whichever provider is configured, on the record.

    The ledger entry is written whether or not the assessment succeeded, with
    the cost the provider reported — zero for a failure today. A call that was
    made is a call that happened, and the row is what makes a failure rate
    visible in /admin/costs rather than only in the logs (CLAUDE.md section 8).
    """
    scorer = get_scorer(target.language, settings=settings)
    ledger = CostLedger(session_factory=session_factory, settings=settings)

    async with ledger.external_call(provider=scorer.name, ref="attempt", user_id=user_id) as entry:
        result = await scorer.assess(
            payload,
            language=target.language,
            mode=target.mode,
            reference_text=target.reference_text,
        )
        entry.record(unit="calls", quantity=1, cost_usd_cents=result.cost_usd_cents)
    return result


async def _record_attempt(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    target: _Target,
    result: PronunciationResult,
    passed: bool,
    provider: str,
) -> uuid.UUID:
    """Write the attempts row.

    ``audio_url`` stays null: the recording is not kept. There is no object
    storage yet (D-002), and a learner's voice is not something to start
    retaining without deciding to.
    """
    statement = (
        sa.insert(Attempt)
        .values(
            user_id=user_id,
            lesson_item_id=target.item.id,
            concept_id=target.item.concept_id,
            asr_text=result.asr_text or None,
            pron_score=result.pron_score,
            accuracy_score=result.accuracy_score,
            fluency_score=result.fluency_score,
            completeness_score=result.completeness_score,
            prosody_score=result.prosody_score,
            tone_score=result.tone_score,
            phoneme_detail=result.raw or None,
            passed=passed,
            provider=provider,
        )
        .returning(Attempt.id)
    )
    attempt_id: uuid.UUID = (await session.execute(statement)).scalar_one()
    return attempt_id


async def _advance_mastery(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    target: _Target,
    pron_score: float,
    now: datetime.datetime,
    settings: Settings,
) -> tuple[MasteryUpdate, ReviewSchedule] | None:
    """Move the concept's mastery and decide when it comes back.

    None when the item teaches no concept — vocab drills without one exist in
    the schema, and there is nothing to advance.

    The row is seeded and then locked before it is read. Two attempts on one
    concept arriving together would otherwise both read the old score and the
    second would overwrite the first's progress: no error, no log line, one
    attempt's worth of learning gone. Seeding first means the lock always has
    something to take.
    """
    concept_id = target.item.concept_id
    if concept_id is None:
        return None

    await session.execute(
        pg_insert(ConceptMastery)
        .values(
            user_id=user_id,
            concept_id=concept_id,
            mastery_score=0.0,
            attempt_count=0,
            # L-6: never let the DDL default supply this. It says 2.5 and
            # SM2_EASE_INITIAL happens to say 2.5 too, so the day anyone
            # changes the setting, rows created this way would keep the old
            # value and nothing would fail.
            ease_factor=initial_ease_factor(settings),
            interval_days=INITIAL_INTERVAL_DAYS,
        )
        .on_conflict_do_nothing(index_elements=["user_id", "concept_id"])
    )
    stored = (
        await session.execute(
            sa.select(ConceptMastery)
            .where(
                ConceptMastery.user_id == user_id,
                ConceptMastery.concept_id == concept_id,
            )
            .with_for_update()
        )
    ).scalar_one()

    update = apply_attempt(
        current_mastery=stored.mastery_score,
        pron_score=pron_score,
        item_type=target.item.item_type,
        settings=settings,
    )
    schedule = schedule_review(
        mastery=update.current,
        ease_factor=stored.ease_factor,
        interval_days=stored.interval_days,
        now=now,
        settings=settings,
    )

    await session.execute(
        sa.update(ConceptMastery)
        .where(
            ConceptMastery.user_id == user_id,
            ConceptMastery.concept_id == concept_id,
        )
        .values(
            mastery_score=update.current,
            attempt_count=ConceptMastery.attempt_count + 1,
            ease_factor=schedule.ease_factor,
            interval_days=schedule.interval_days,
            last_seen_at=now,
            next_due_at=schedule.next_due_at,
        )
    )
    return update, schedule


def _attempt_out(
    attempt_id: uuid.UUID,
    *,
    target: _Target,
    result: PronunciationResult,
    passed: bool,
    mastery: tuple[MasteryUpdate, ReviewSchedule] | None,
    remaining_attempts: int | None,
) -> AttemptOut:
    concept_id = target.item.concept_id
    return AttemptOut(
        id=attempt_id,
        passed=passed,
        scores=ScoresOut(
            pron=result.pron_score,
            accuracy=result.accuracy_score,
            fluency=result.fluency_score,
            completeness=result.completeness_score,
            prosody=result.prosody_score,
            tone=result.tone_score,
        ),
        words=[
            WordScoreOut(
                word=word.word,
                accuracy=word.accuracy,
                error_type=word.error_type,
                phonemes=[
                    PhonemeOut(
                        phoneme=phoneme.phoneme,
                        accuracy=phoneme.accuracy,
                        offset_ms=phoneme.offset_ms,
                        duration_ms=phoneme.duration_ms,
                    )
                    for phoneme in word.phonemes
                ],
            )
            for word in result.words
        ],
        feedback=FeedbackOut(
            key=FEEDBACK_PASSED_KEY if passed else FEEDBACK_RETRY_KEY,
            km_explanation=target.km_explanation,
        ),
        mastery=(
            None
            if mastery is None or concept_id is None
            else MasteryOut(
                concept_id=concept_id,
                previous=mastery[0].previous,
                current=mastery[0].current,
                interval_days=mastery[1].interval_days,
                next_due_at=mastery[1].next_due_at,
            )
        ),
        remaining_attempts=remaining_attempts,
    )


__all__ = ["router"]
