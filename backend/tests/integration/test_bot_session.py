"""Where a learner left off, stored in Redis (BACKLOG D6).

The first thing in the repository that connects to Redis, which is what
discharges carry-forward constraint L-2: the CI workflow gained a redis service
in the same change, and the fixture refuses to skip under CI.

What is worth testing here is not "a write can be read back" but the two ways
stored state goes wrong: it expires, and it survives a deploy that changed its
shape.
"""

from __future__ import annotations

import uuid

import pytest
from bot.flow import Session, Step
from bot.session import SessionStore, key_for
from redis.asyncio import Redis

pytestmark = pytest.mark.integration

TELEGRAM_ID = 880001


@pytest.fixture
def store(redis_client: Redis) -> SessionStore:
    return SessionStore(redis_client, ttl_seconds=3600)


async def test_nothing_is_stored_for_a_new_learner(store: SessionStore) -> None:
    assert await store.load(TELEGRAM_ID) is None


async def test_a_session_survives_the_round_trip(store: SessionStore) -> None:
    session = Session(lesson_id=uuid.uuid4(), step_index=2, attempts_used=1)

    await store.save(TELEGRAM_ID, session)

    assert await store.load(TELEGRAM_ID) == session


async def test_learners_do_not_share_a_place(store: SessionStore) -> None:
    mine = Session(lesson_id=uuid.uuid4(), step_index=3)
    await store.save(TELEGRAM_ID, mine)

    assert await store.load(TELEGRAM_ID + 1) is None


async def test_clearing_forgets_the_lesson(store: SessionStore) -> None:
    await store.save(TELEGRAM_ID, Session(lesson_id=uuid.uuid4()))

    await store.clear(TELEGRAM_ID)

    assert await store.load(TELEGRAM_ID) is None


async def test_a_stored_session_expires(store: SessionStore, redis_client: Redis) -> None:
    """An abandoned lesson clears itself rather than waiting for a cleanup job."""
    await store.save(TELEGRAM_ID, Session(lesson_id=uuid.uuid4()))

    ttl = await redis_client.ttl(key_for(TELEGRAM_ID))

    assert 0 < ttl <= 3600


async def test_working_through_a_lesson_keeps_pushing_the_expiry_back(
    redis_client: Redis,
) -> None:
    short = SessionStore(redis_client, ttl_seconds=100)
    await short.save(TELEGRAM_ID, Session(lesson_id=uuid.uuid4()))
    await redis_client.expire(key_for(TELEGRAM_ID), 5)

    await short.save(TELEGRAM_ID, Session(lesson_id=uuid.uuid4(), step_index=1))

    assert await redis_client.ttl(key_for(TELEGRAM_ID)) > 5


@pytest.mark.parametrize(
    "stored",
    [
        b"not json at all",
        b"{}",
        b'{"lesson_id": "not-a-uuid", "step_index": 0, "attempts_used": 0}',
        b'{"lesson_id": "0d2b0a2e-0000-4000-8000-000000000000"}',
        b'{"step_index": 1, "attempts_used": 0}',
    ],
)
async def test_unreadable_state_is_treated_as_no_lesson(
    store: SessionStore, redis_client: Redis, stored: bytes
) -> None:
    """A deploy that changed the stored shape must not make every message
    raise. The learner starts the lesson again, which is recoverable."""
    await redis_client.set(key_for(TELEGRAM_ID), stored)

    assert await store.load(TELEGRAM_ID) is None


async def test_unreadable_state_is_thrown_away_rather_than_left_to_rot(
    store: SessionStore, redis_client: Redis
) -> None:
    await redis_client.set(key_for(TELEGRAM_ID), b"garbage")

    await store.load(TELEGRAM_ID)

    assert await redis_client.get(key_for(TELEGRAM_ID)) is None


async def test_a_review_s_steps_survive_the_round_trip(store: SessionStore) -> None:
    """A review is not a lesson the API can be asked for again (D-133)."""
    steps = (
        Step(
            item_id=uuid.uuid4(),
            item_type="qa",
            text="你要什么?",
            audio_url="https://cdn.example/q.mp3",
            answer_text="我要水。",
            answer_audio_url="https://cdn.example/a.mp3",
        ),
        Step(item_id=uuid.uuid4(), item_type="drill", text="我要水。"),
    )
    session = Session(lesson_id=uuid.uuid4(), step_index=1, review_steps=steps)

    await store.save(TELEGRAM_ID, session)

    assert await store.load(TELEGRAM_ID) == session


async def test_a_session_stored_before_reviews_existed_still_loads(
    store: SessionStore, redis_client: Redis
) -> None:
    lesson_id = uuid.uuid4()
    await redis_client.set(
        key_for(TELEGRAM_ID),
        f'{{"lesson_id": "{lesson_id}", "step_index": 2, "attempts_used": 1}}',
    )

    assert await store.load(TELEGRAM_ID) == Session(lesson_id, step_index=2, attempts_used=1)


async def test_a_masked_round_and_a_drill_s_parts_survive_the_round_trip(
    store: SessionStore,
) -> None:
    """D-135: resuming mid-drill must not drop the learner back to the full sentence."""
    drill = Step(
        item_id=uuid.uuid4(), item_type="drill", text="t", target_text="我要水。", hint="ទឹក"
    )
    session = Session(lesson_id=uuid.uuid4(), mask_round=2, review_steps=(drill,))

    await store.save(TELEGRAM_ID, session)

    assert await store.load(TELEGRAM_ID) == session
