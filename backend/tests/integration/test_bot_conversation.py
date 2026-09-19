"""A learner walks through a lesson in Telegram (BACKLOG D6).

    explain -> drill -> vocab -> Q&A -> complete, with voice notes both ways.

The conversation runs against the real API — the same app D1 through D5 built,
with every provider fake — over an in-process transport, and against a real
Redis and a real database. The only thing simulated is Telegram itself: the
handlers take a learner id and some audio bytes, which is the whole reason
bot/handlers.py keeps Telegram types out of Conversation.

The acceptance is "walk a whole unit in real Telegram with fake providers".
This is that walk, minus Telegram's own delivery: what it proves is that the
bot, the API, the scorer, the ledger, mastery and the review schedule agree
with each other end to end.
"""

from __future__ import annotations

import uuid
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
from fastapi import FastAPI
from redis.asyncio import Redis

from app.core.config import Settings
from app.main import create_app
from tests.integration.conftest import Execute, Rows
from tests.unit.test_security import BOT_TOKEN

pytestmark = pytest.mark.integration

AUDIO = (
    Path(__file__).resolve().parents[1] / "fixtures" / "audio" / "drill_short.wav"
).read_bytes()

LEARNER = Learner(telegram_id=770001, locale="en", language_code="en")


@pytest.fixture
def unit(db: sa.Engine) -> dict[str, uuid.UUID]:
    """One HSK1 lesson shaped like PRD 3.3: explain, drill, vocab, Q&A."""
    ids = {
        name: uuid.uuid4()
        for name in ("course", "lesson", "concept", "explain", "drill", "vocab", "qa")
    }
    with db.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO concepts (id, slug, language, level, pattern, km_explanation) "
                "VALUES (:id, 'zh.hsk1.want_noun', 'zh', 'HSK1', '我要 + [名词]', 'ការពន្យល់')"
            ),
            {"id": ids["concept"]},
        )
        conn.execute(
            sa.text(
                "INSERT INTO courses (id, course_type, language, level, title_km) "
                "VALUES (:id, 'exam', 'zh', 'HSK1', 'ភាសាចិន HSK1')"
            ),
            {"id": ids["course"]},
        )
        conn.execute(
            sa.text(
                "INSERT INTO lessons (id, course_id, concept_ids, sequence, title_km) "
                "VALUES (:id, :course, :concepts, 1, 'មេរៀនទី ១')"
            ),
            {"id": ids["lesson"], "course": ids["course"], "concepts": [ids["concept"]]},
        )
        conn.execute(
            sa.text(
                "INSERT INTO lesson_items "
                "(id, lesson_id, concept_id, item_type, payload, sequence) "
                "VALUES (:id, :lesson, :concept, :type, CAST(:payload AS jsonb), :seq)"
            ),
            [
                {
                    "id": ids["explain"],
                    "lesson": ids["lesson"],
                    "concept": ids["concept"],
                    "type": "explain",
                    "payload": '{"text": "我要 + noun"}',
                    "seq": 1,
                },
                {
                    "id": ids["drill"],
                    "lesson": ids["lesson"],
                    "concept": ids["concept"],
                    "type": "drill",
                    "payload": '{"target_text": "我要水"}',
                    "seq": 2,
                },
                {
                    "id": ids["vocab"],
                    "lesson": ids["lesson"],
                    "concept": ids["concept"],
                    "type": "vocab",
                    "payload": '{"target_text": "我要茶"}',
                    "seq": 3,
                },
                {
                    "id": ids["qa"],
                    "lesson": ids["lesson"],
                    "concept": ids["concept"],
                    "type": "qa",
                    "payload": '{"prompt": "你要什么?"}',
                    "seq": 4,
                },
            ],
        )
        conn.execute(
            sa.text(
                "INSERT INTO media_assets "
                "(lesson_item_id, kind, url, provider, source_text_hash) "
                "VALUES (:item, 'audio', 'https://cdn.example/explain.mp3', 'fake', 'h')"
            ),
            {"item": ids["explain"]},
        )
    return ids


@pytest.fixture
async def conversation(settings: Settings, redis_client: Redis) -> AsyncIterator[Conversation]:
    """The bot, wired to the real API over an in-process transport."""
    app: FastAPI = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://kruai.test") as http:
            yield Conversation(
                KruaiApi(http, bot_token=BOT_TOKEN),
                SessionStore(redis_client, ttl_seconds=settings.bot_session_ttl_seconds),
                max_retry=settings.scoring_max_retry,
            )


def texts(messages: list[Any]) -> list[str]:
    return [message.text for message in messages]


# ------------------------------------------------------------------ the walk


async def test_a_learner_walks_a_whole_unit(
    conversation: Conversation, unit: dict[str, uuid.UUID], rows: Rows
) -> None:
    """The D6 acceptance: explain, drill, vocab, Q&A, complete."""
    opening = await conversation.on_learn(LEARNER)
    assert "មេរៀនទី ១" in opening[0].text
    assert "我要 + noun" in opening[1].text
    assert opening[1].audio_url == "https://cdn.example/explain.mp3"
    assert "我要水" in opening[2].text

    drill = await conversation.on_voice(LEARNER, audio=AUDIO)
    assert "我要茶" in drill[-1].text

    vocab = await conversation.on_voice(LEARNER, audio=AUDIO)
    assert "你要什么?" in vocab[-1].text

    final = await conversation.on_voice(LEARNER, audio=AUDIO)
    assert "Lesson finished" in final[-1].text

    assert rows("SELECT count(*) FROM attempts") == [(3,)]
    assert rows("SELECT status FROM lesson_progress") == [("completed",)]
    assert rows("SELECT count(*) FROM cost_ledger") == [(3,)]
    assert rows("SELECT attempt_count FROM concept_mastery") == [(3,)]
    assert rows("SELECT next_due_at IS NOT NULL FROM concept_mastery") == [(True,)]


async def test_the_session_is_forgotten_once_the_lesson_ends(
    conversation: Conversation, unit: dict[str, uuid.UUID], redis_client: Redis
) -> None:
    await conversation.on_learn(LEARNER)
    for _ in range(3):
        await conversation.on_voice(LEARNER, audio=AUDIO)

    assert await redis_client.get(f"kruai:bot:session:{LEARNER.telegram_id}") is None
    assert "No lesson in progress" in (await conversation.on_voice(LEARNER, audio=AUDIO))[0].text


async def test_a_lesson_survives_the_bot_restarting(
    settings: Settings, unit: dict[str, uuid.UUID], redis_client: Redis
) -> None:
    """The M2 checklist asks for state that outlives a restart. Two separate
    Conversation objects share nothing but Redis."""

    @asynccontextmanager
    async def fresh_bot() -> AsyncIterator[Conversation]:
        """A bot process that shares nothing with the previous one but Redis."""
        app: FastAPI = create_app(settings)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://k.test") as http:
                yield Conversation(
                    KruaiApi(http, bot_token=BOT_TOKEN),
                    SessionStore(redis_client, ttl_seconds=settings.bot_session_ttl_seconds),
                    max_retry=settings.scoring_max_retry,
                )

    async with fresh_bot() as before:
        await before.on_learn(LEARNER)
        await before.on_voice(LEARNER, audio=AUDIO)

    async with fresh_bot() as after:
        resumed = await after.on_learn(LEARNER)

    assert "Continuing" in resumed[0].text
    assert "step 3 of 4" in resumed[0].text


async def test_stopping_puts_the_lesson_down_but_keeps_the_progress(
    conversation: Conversation, unit: dict[str, uuid.UUID], rows: Rows
) -> None:
    await conversation.on_learn(LEARNER)
    await conversation.on_voice(LEARNER, audio=AUDIO)

    stopped = await conversation.on_stop(LEARNER)
    after = await conversation.on_voice(LEARNER, audio=AUDIO)

    assert "Lesson stopped" in stopped[0].text
    assert "No lesson in progress" in after[0].text
    assert rows("SELECT count(*) FROM attempts") == [(1,)], "the attempt already made stands"


async def test_a_voice_note_before_any_lesson_is_explained_not_scored(
    conversation: Conversation, unit: dict[str, uuid.UUID], rows: Rows
) -> None:
    replies = await conversation.on_voice(LEARNER, audio=AUDIO)

    assert "No lesson in progress" in replies[0].text
    assert rows("SELECT count(*) FROM attempts") == [(0,)]
    assert rows("SELECT count(*) FROM cost_ledger") == [(0,)], "nothing was sent to a scorer"


# --------------------------------------------------------------- refusals


async def test_an_exhausted_allowance_is_explained_rather_than_retried(
    conversation: Conversation, unit: dict[str, uuid.UUID], execute: Execute, rows: Rows
) -> None:
    await conversation.on_learn(LEARNER)
    execute("UPDATE entitlements SET daily_attempts_used = 10")

    replies = await conversation.on_voice(LEARNER, audio=AUDIO)

    assert "allowance is used up" in replies[0].text
    assert rows("SELECT count(*) FROM attempts") == [(0,)]


async def test_a_scoring_failure_asks_for_the_recording_again(
    conversation: Conversation, unit: dict[str, uuid.UUID], rows: Rows
) -> None:
    """An empty recording is what FakeScorer treats as a provider failure."""
    await conversation.on_learn(LEARNER)

    replies = await conversation.on_voice(LEARNER, audio=b"")

    assert "Scoring is unavailable" in replies[0].text
    assert rows("SELECT daily_attempts_used FROM entitlements") == [(0,)]


async def test_an_internal_code_never_reaches_the_learner(
    conversation: Conversation, unit: dict[str, uuid.UUID], execute: Execute
) -> None:
    await conversation.on_learn(LEARNER)
    execute("UPDATE entitlements SET daily_attempts_used = 10")

    replies = await conversation.on_voice(LEARNER, audio=AUDIO)

    assert "quota.insufficient" not in replies[0].text
    assert "402" not in replies[0].text


async def test_an_empty_catalogue_says_so(conversation: Conversation) -> None:
    replies = await conversation.on_learn(LEARNER)

    assert "No lessons are available" in replies[0].text


# ------------------------------------------------------------ the other commands


async def test_start_explains_itself(conversation: Conversation) -> None:
    replies = await conversation.on_start(LEARNER)

    assert "/learn" in replies[1].text


async def test_status_reports_the_allowance_from_the_api(
    conversation: Conversation, unit: dict[str, uuid.UUID]
) -> None:
    await conversation.on_learn(LEARNER)
    await conversation.on_voice(LEARNER, audio=AUDIO)

    replies = await conversation.on_status(LEARNER)

    assert "Plan: free" in replies[0].text
    assert "Attempts today: 1 of 10" in replies[0].text


async def test_status_on_a_paid_plan_does_not_say_a_limit(
    conversation: Conversation, execute: Execute, rows: Rows
) -> None:
    """Unlimited comes back from the API as null, and the sentence drops the
    "of N" rather than printing a zero (D-046)."""
    await conversation.on_status(LEARNER)  # provisions the account
    user_id = rows("SELECT id FROM users WHERE telegram_id = :t", t=LEARNER.telegram_id)[0][0]
    execute(
        "INSERT INTO subscriptions (user_id, plan, status, period_start, period_end) "
        "VALUES (:u, 'basic', 'active', now(), now() + interval '30 days')",
        u=user_id,
    )

    replies = await conversation.on_status(LEARNER)

    assert "Plan: basic" in replies[0].text
    assert " of " not in replies[0].text


# ------------------------------------------------------------- authentication


async def test_the_bot_authenticates_as_the_learner_it_is_talking_to(
    conversation: Conversation, unit: dict[str, uuid.UUID], rows: Rows
) -> None:
    """The bot signs its own initData with the bot token, and the API verifies
    it exactly as it verifies a Mini App's (D-048). Two learners, two accounts."""
    other = Learner(telegram_id=770002, locale="en", language_code="en")

    await conversation.on_learn(LEARNER)
    await conversation.on_learn(other)

    stored = rows("SELECT telegram_id FROM users ORDER BY telegram_id")
    assert stored == [(LEARNER.telegram_id,), (other.telegram_id,)]
