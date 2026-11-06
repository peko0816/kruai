"""From a draft to a finished lesson, with nothing hand-written in between (D-130).

Every other test of the learning loop starts from rows a test inserted, and
each of those tests wrote the payload it expected to read. That is how the
importer and the API came to disagree about a payload key without a single
test failing: against real imported content every drill crashed the attempts
endpoint and every step in the bot was blank.

So this one starts where real content starts — a draft — and goes through the
same functions `make build` and `make import` run, then lets the bot walk the
lesson against the real API. All providers are fake; the seam is the point.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from bot.api_client import KruaiApi
from bot.handlers import Conversation, Learner
from bot.session import SessionStore
from pipeline.build_pack import build
from pipeline.import_pack import import_pack
from pipeline.storage import LocalMediaStore
from pipeline.tests.test_build_pack import a_draft
from redis.asyncio import Redis
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.main import create_app
from tests.integration.conftest import Execute, Rows
from tests.unit.test_security import BOT_TOKEN

pytestmark = pytest.mark.integration

AUDIO = (
    Path(__file__).resolve().parents[1] / "fixtures" / "audio" / "drill_short.wav"
).read_bytes()

LEARNER = Learner(telegram_id=770101, locale="en", language_code="en")
CDN = "https://cdn.example"


@pytest.fixture
async def imported(settings: Settings, db: sa.Engine, tmp_path: Path) -> None:
    """One concept, built and imported exactly as `make build` / `make import` would."""
    pack = await build(
        a_draft(concepts=1),
        settings=settings,
        media_dir=tmp_path / "media",
        version="v-e2e",
        validated=True,
    )
    with Session(db) as session, session.begin():
        import_pack(
            pack,
            session=session,
            store=LocalMediaStore(tmp_path / "published", base_url=CDN),
            media_dir=tmp_path / "media",
        )


@asynccontextmanager
async def bot_for(
    settings: Settings, redis_client: Redis, *, masked_rounds: int = 0
) -> AsyncIterator[Conversation]:
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://kruai.test") as http:
            bot = Conversation(
                KruaiApi(http, bot_token=BOT_TOKEN),
                SessionStore(redis_client, ttl_seconds=settings.bot_session_ttl_seconds),
                max_retry=settings.scoring_max_retry,
                masked_rounds=masked_rounds,
            )
            # These assertions read English. An English Telegram starts in
            # Khmer (D-148), so the learner picks English first, as one would.
            await bot.on_language(LEARNER, "en")
            yield bot


@pytest.fixture
async def conversation(settings: Settings, redis_client: Redis) -> AsyncIterator[Conversation]:
    async with bot_for(settings, redis_client) as bot:
        yield bot


async def test_imported_content_can_be_learned_end_to_end(
    imported: None, conversation: Conversation, rows: Rows
) -> None:
    draft = a_draft(concepts=1).concepts[0]

    opening = await conversation.on_learn(LEARNER)
    explain, first_prompt = opening[1], opening[-1]
    # The learner reads the explanation, not an empty bubble with audio on it.
    assert draft.km_explanation in explain.text
    assert draft.pattern in explain.text
    assert explain.audio_url is not None and explain.audio_url.startswith(CDN)
    # The drill shows the sentence to say, its reading and its meaning.
    sentence = draft.target_sentences[0]
    assert sentence.zh in first_prompt.text
    assert sentence.pinyin in first_prompt.text

    after_drill = await conversation.on_voice(LEARNER, audio=AUDIO)
    assert draft.substitutions[0].zh in after_drill[-1].text

    after_vocab = await conversation.on_voice(LEARNER, audio=AUDIO)
    assert draft.dialogues[0].prompt_zh in after_vocab[-1].text

    finished = await conversation.on_voice(LEARNER, audio=AUDIO)
    assert "Lesson finished" in finished[-1].text
    # The model answer, shown and played once the question is behind them (D-132).
    answer = next(m for m in finished if m.text == draft.dialogues[0].sample_answer_zh)
    assert answer.audio_url is not None and answer.audio_url.startswith(CDN)

    # Scored, not crashed: one attempt per spoken step, and the lesson closed.
    assert rows("SELECT count(*) FROM attempts") == [(3,)]
    assert rows("SELECT status FROM lesson_progress") == [("completed",)]


# ------------------------------------------------------------------ review (D-133)


async def finish_the_lesson(conversation: Conversation) -> None:
    await conversation.on_learn(LEARNER)
    for _ in range(3):
        await conversation.on_voice(LEARNER, audio=AUDIO)


async def test_nothing_due_says_so(imported: None, conversation: Conversation, rows: Rows) -> None:
    await finish_the_lesson(conversation)

    replies = await conversation.on_review(LEARNER)

    assert "Nothing is due" in replies[0].text


def first_line(message: Any) -> str:
    """What the learner is asked to say: exact, because 水 is inside 我要水。"""
    return str(message.text).splitlines()[0]


def as_tester(settings: Settings, **overrides: Any) -> Settings:
    """Free allows three recordings a day and the lesson uses them; a trial
    tester is Basic (D-131), which is who will be reviewing during the trial."""
    return settings.model_copy(update={"trial_telegram_ids": (LEARNER.telegram_id,), **overrides})


async def test_a_due_concept_comes_back_through_review(
    imported: None, settings: Settings, redis_client: Redis, rows: Rows, execute: Execute
) -> None:
    """Smart Review, reachable from the bot: the drill and the vocab item of the
    concept, scored like any attempt, and no lesson to complete at the end."""
    draft = a_draft(concepts=1).concepts[0]
    async with bot_for(as_tester(settings), redis_client) as bot:
        await finish_the_lesson(bot)
        execute("UPDATE concept_mastery SET next_due_at = now() - interval '1 day'")

        opening = await bot.on_review(LEARNER)
        assert "Review — 2 recordings." in opening[0].text
        assert first_line(opening[-1]) == f"Say this: {draft.target_sentences[0].zh}"

        await bot.on_voice(LEARNER, audio=AUDIO)
        finished = await bot.on_voice(LEARNER, audio=AUDIO)

    assert finished[-1].text == "Review finished."
    assert rows("SELECT count(*) FROM attempts") == [(5,)]
    assert rows("SELECT count(*) FROM lesson_progress") == [(1,)], "a review is not a lesson"


async def test_a_review_rotates_through_a_concept_s_items(
    imported: None, settings: Settings, redis_client: Redis, rows: Rows, execute: Execute
) -> None:
    """One item per concept: the one practised longest ago, so the next review
    picks up the other rather than repeating the first."""
    draft = a_draft(concepts=1).concepts[0]
    async with bot_for(as_tester(settings, review_items_per_concept=1), redis_client) as bot:
        await finish_the_lesson(bot)
        execute("UPDATE concept_mastery SET next_due_at = now() - interval '1 day'")
        first = await bot.on_review(LEARNER)
        await bot.on_voice(LEARNER, audio=AUDIO)
        execute("UPDATE concept_mastery SET next_due_at = now() - interval '1 day'")
        second = await bot.on_review(LEARNER)

    assert "Review — 1 recordings." in first[0].text, "REVIEW_ITEMS_PER_CONCEPT=1"
    # The drill was spoken before the vocab item in the lesson, so it is older.
    assert first_line(first[-1]) == f"Say this: {draft.target_sentences[0].zh}"
    assert first_line(second[-1]) == f"Say this: {draft.substitutions[0].zh}"
    assert rows("SELECT count(*) FROM attempts") == [(4,)], "the review attempt was scored"


async def test_a_never_practised_item_comes_before_a_practised_one(
    imported: None, settings: Settings, redis_client: Redis, execute: Execute
) -> None:
    """Say the learner left the lesson before the vocab item: that item is the
    one the concept most needs, so it leads, ahead of the drill they did say."""
    draft = a_draft(concepts=1).concepts[0]
    async with bot_for(as_tester(settings, review_items_per_concept=1), redis_client) as bot:
        await finish_the_lesson(bot)
        execute(
            "DELETE FROM attempts WHERE lesson_item_id IN "
            "(SELECT id FROM lesson_items WHERE item_type = 'vocab')"
        )
        execute("UPDATE concept_mastery SET next_due_at = now() - interval '1 day'")
        review = await bot.on_review(LEARNER)

    assert first_line(review[-1]) == f"Say this: {draft.substitutions[0].zh}"


async def test_learn_puts_a_review_down_and_opens_the_lesson(
    imported: None, conversation: Conversation, execute: Execute
) -> None:
    await finish_the_lesson(conversation)
    execute("UPDATE concept_mastery SET next_due_at = now() - interval '1 day'")
    await conversation.on_review(LEARNER)

    replies = await conversation.on_learn(LEARNER)

    # The only lesson is done, so /learn says so — rather than trying to load
    # the review as if it were a lesson, which the API does not know.
    assert "finished every lesson" in replies[0].text


# -------------------------------------------------------------- masked drills (D-135)


async def test_a_drill_is_said_in_full_then_twice_from_memory(
    imported: None, settings: Settings, redis_client: Redis, rows: Rows
) -> None:
    """PRD 3.1's 跟读→逐步遮词 on imported content: the same sentence three times,
    each scored against the whole of it, and then the lesson carries on."""
    draft = a_draft(concepts=1).concepts[0]
    sentence = draft.target_sentences[0]
    hidden = "\uff3f"
    async with bot_for(as_tester(settings), redis_client, masked_rounds=2) as bot:
        await bot.on_learn(LEARNER)
        first = await bot.on_voice(LEARNER, audio=AUDIO)
        second = await bot.on_voice(LEARNER, audio=AUDIO)
        third = await bot.on_voice(LEARNER, audio=AUDIO)

    assert first_line(first[-1]).count(hidden) == 2, "half of 我要X hidden"
    assert sentence.pinyin not in first[-1].text
    assert sentence.km_gloss in first[-1].text, "the meaning stays as the cue"
    assert first[-1].audio_url is None
    assert first_line(second[-1]).count(hidden) == 3, "all of it hidden"
    assert first_line(third[-1]) == f"Say this: {draft.substitutions[0].zh}"
    assert rows("SELECT count(*) FROM attempts") == [(3,)], "three scored recordings, one drill"


async def test_coming_back_mid_round_still_hides_the_words(
    imported: None, settings: Settings, redis_client: Redis
) -> None:
    """/learn after a break resumes the round reached, gaps and all — not the
    full sentence, which would be the answer."""
    hidden = "\uff3f"
    async with bot_for(as_tester(settings), redis_client, masked_rounds=2) as bot:
        await bot.on_learn(LEARNER)
        await bot.on_voice(LEARNER, audio=AUDIO)  # said in full; now in round one
        resumed = await bot.on_learn(LEARNER)

    assert first_line(resumed[-1]).count(hidden) == 2
