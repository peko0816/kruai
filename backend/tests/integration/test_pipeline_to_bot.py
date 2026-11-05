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
from pathlib import Path

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
from tests.integration.conftest import Rows
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


@pytest.fixture
async def conversation(settings: Settings, redis_client: Redis) -> AsyncIterator[Conversation]:
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://kruai.test") as http:
            yield Conversation(
                KruaiApi(http, bot_token=BOT_TOKEN),
                SessionStore(redis_client, ttl_seconds=settings.bot_session_ttl_seconds),
                max_retry=settings.scoring_max_retry,
            )


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

    # Scored, not crashed: one attempt per spoken step, and the lesson closed.
    assert rows("SELECT count(*) FROM attempts") == [(3,)]
    assert rows("SELECT status FROM lesson_progress") == [("completed",)]
