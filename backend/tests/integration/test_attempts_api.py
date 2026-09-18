"""POST /api/v1/attempts, the whole chain (BACKLOG D3).

    quota -> scoring -> cost_ledger -> attempts row -> mastery -> schedule

Two acceptance criteria, and the second is the one worth the tests: a run with
FakeScorer and a fixed recording goes end to end, and **a scoring failure
charges nothing and stores nothing**. FakeScorer fails on demand — a reference
text containing ``__BAD__`` scores low, an empty recording comes back ok=False
(ARCHITECTURE 3.2) — so that branch is reachable without breaking a vendor.

The recordings are the committed samples in tests/fixtures/audio. FakeScorer's
score depends on the reference text and the length of the audio, so a fixed
file means a fixed score: a number that moves here is the code moving.
"""

from __future__ import annotations

import datetime
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa

from app.core.config import Settings
from app.services.scoring.fake import BAD_MARKER
from tests.integration.conftest import Execute, Rows, auth_header, client_for, sign_in, token_for

pytestmark = pytest.mark.integration

ATTEMPTS_URL = "/api/v1/attempts"

AUDIO_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "audio"
SHORT_AUDIO = (AUDIO_DIR / "drill_short.wav").read_bytes()
LONG_AUDIO = (AUDIO_DIR / "drill_long.wav").read_bytes()

TARGET_TEXT = "我要水"


@pytest.fixture
def seeded(db: sa.Engine) -> dict[str, uuid.UUID]:
    """One HSK1 lesson with a drill, a lecture card, a failing drill and a Q&A.

    The failing drill carries FakeScorer's ``__BAD__`` marker in its target
    text, which is how the below-threshold path is exercised without a second
    scorer.
    """
    ids = {
        name: uuid.uuid4()
        for name in (
            "org",
            "course",
            "org_course",
            "lesson",
            "org_lesson",
            "concept",
            "drill",
            "explain",
            "failing_drill",
            "qa",
            "orphan_drill",
            "org_drill",
            "broken_drill",
        )
    }
    with db.begin() as conn:
        conn.execute(
            sa.text("INSERT INTO orgs (id, name, seats) VALUES (:id, 'Hotel Co', 5)"),
            {"id": ids["org"]},
        )
        conn.execute(
            sa.text(
                "INSERT INTO concepts (id, slug, language, level, pattern, km_explanation) "
                "VALUES (:id, 'zh.hsk1.want_noun', 'zh', 'HSK1', '我要 + [名词]', 'ការពន្យល់')"
            ),
            {"id": ids["concept"]},
        )
        conn.execute(
            sa.text(
                "INSERT INTO courses (id, course_type, language, level, title_km, org_id) "
                "VALUES (:id, 'exam', 'zh', 'HSK1', 'ភាសាចិន', :org)"
            ),
            [
                {"id": ids["course"], "org": None},
                {"id": ids["org_course"], "org": ids["org"]},
            ],
        )
        conn.execute(
            sa.text(
                "INSERT INTO lessons (id, course_id, concept_ids, sequence, title_km) "
                "VALUES (:id, :course, :concepts, 1, 'មេរៀន')"
            ),
            [
                {"id": ids["lesson"], "course": ids["course"], "concepts": [ids["concept"]]},
                {"id": ids["org_lesson"], "course": ids["org_course"], "concepts": []},
            ],
        )
        conn.execute(
            sa.text(
                "INSERT INTO lesson_items "
                "(id, lesson_id, concept_id, item_type, payload, sequence) "
                "VALUES (:id, :lesson, :concept, :type, CAST(:payload AS jsonb), :sequence)"
            ),
            [
                {
                    "id": ids["drill"],
                    "lesson": ids["lesson"],
                    "concept": ids["concept"],
                    "type": "drill",
                    "payload": f'{{"target_text": "{TARGET_TEXT}"}}',
                    "sequence": 1,
                },
                {
                    "id": ids["explain"],
                    "lesson": ids["lesson"],
                    "concept": ids["concept"],
                    "type": "explain",
                    "payload": '{"km": "ការពន្យល់"}',
                    "sequence": 2,
                },
                {
                    "id": ids["failing_drill"],
                    "lesson": ids["lesson"],
                    "concept": ids["concept"],
                    "type": "drill",
                    "payload": f'{{"target_text": "{TARGET_TEXT}{BAD_MARKER}"}}',
                    "sequence": 3,
                },
                {
                    "id": ids["qa"],
                    "lesson": ids["lesson"],
                    "concept": ids["concept"],
                    "type": "qa",
                    "payload": '{"prompt_km": "សំណួរ"}',
                    "sequence": 4,
                },
                {
                    "id": ids["orphan_drill"],
                    "lesson": ids["lesson"],
                    "concept": None,
                    "type": "drill",
                    "payload": f'{{"target_text": "{TARGET_TEXT}"}}',
                    "sequence": 5,
                },
                {
                    "id": ids["broken_drill"],
                    "lesson": ids["lesson"],
                    "concept": ids["concept"],
                    "type": "drill",
                    "payload": '{"note": "the pack forgot the target sentence"}',
                    "sequence": 6,
                },
                {
                    "id": ids["org_drill"],
                    "lesson": ids["org_lesson"],
                    "concept": None,
                    "type": "drill",
                    "payload": f'{{"target_text": "{TARGET_TEXT}"}}',
                    "sequence": 1,
                },
            ],
        )
    return ids


def upload(item_id: uuid.UUID, audio: bytes = SHORT_AUDIO) -> dict[str, Any]:
    """The multipart body of one attempt."""
    return {
        "data": {"lesson_item_id": str(item_id)},
        "files": {"audio": ("attempt.wav", audio, "audio/wav")},
    }


async def attempt(
    client: httpx.AsyncClient,
    item_id: uuid.UUID,
    *,
    headers: dict[str, str],
    audio: bytes = SHORT_AUDIO,
) -> httpx.Response:
    body = upload(item_id, audio)
    return await client.post(ATTEMPTS_URL, headers=headers, **body)


async def authenticated(client: httpx.AsyncClient, **kwargs: Any) -> dict[str, str]:
    return auth_header(await token_for(client, **kwargs))


# ------------------------------------------------------------------- the chain


async def test_an_attempt_scores_stores_and_advances_mastery(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID], rows: Rows
) -> None:
    """One recording, all six steps."""
    headers = await authenticated(client)

    response = await attempt(client, seeded["drill"], headers=headers)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["passed"] is True
    assert 0 <= body["scores"]["pron"] <= 100
    # zh-CN carries tone and no prosody (PRD section 8).
    assert body["scores"]["tone"] is not None
    assert body["scores"]["prosody"] is None

    stored = rows(
        "SELECT user_id, lesson_item_id, concept_id, pron_score, passed, provider, audio_url "
        "FROM attempts"
    )
    assert len(stored) == 1
    assert stored[0][1] == seeded["drill"]
    assert stored[0][2] == seeded["concept"]
    assert stored[0][4] is True
    assert stored[0][5] == "fake"
    # The recording itself is not kept.
    assert stored[0][6] is None

    mastery = rows("SELECT mastery_score, attempt_count, next_due_at FROM concept_mastery")
    assert len(mastery) == 1
    assert mastery[0][0] > 0
    assert mastery[0][1] == 1
    assert mastery[0][2] is not None


async def test_the_call_is_on_the_ledger(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID], rows: Rows
) -> None:
    """CLAUDE.md section 8: an unledgered external call counts as not happening."""
    headers = await authenticated(client)

    await attempt(client, seeded["drill"], headers=headers)

    ledger = rows("SELECT provider, unit, quantity, cost_usd_cents_est, ref FROM cost_ledger")
    assert ledger == [("fake", "calls", 1.0, 1, "attempt")]


async def test_the_allowance_is_spent(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID], rows: Rows
) -> None:
    headers = await authenticated(client)

    response = await attempt(client, seeded["drill"], headers=headers)

    assert rows("SELECT daily_attempts_used FROM entitlements") == [(1,)]
    # Free plan allows 10 a day; one is gone.
    assert response.json()["remaining_attempts"] == 9


async def test_the_schedule_is_written_so_the_concept_comes_back(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID], rows: Rows
) -> None:
    headers = await authenticated(client)

    body = (await attempt(client, seeded["drill"], headers=headers)).json()

    interval, next_due = rows("SELECT interval_days, next_due_at FROM concept_mastery")[0]
    assert interval >= 1
    assert next_due > datetime.datetime.now(datetime.UTC)
    assert body["mastery"]["interval_days"] == interval


async def test_a_new_row_starts_from_the_configured_ease_not_the_column_default(
    settings: Settings, seeded: dict[str, uuid.UUID], rows: Rows
) -> None:
    """L-6, pinned.

    concept_mastery.ease_factor defaults to 2.5 in the DDL and SM2_EASE_INITIAL
    also says 2.5, so a writer that leaned on the column default would look
    correct forever — right up until someone changed the setting. Configuring a
    different initial is the only way to tell the two apart.

    One attempt on a fresh concept leaves mastery far below MASTERY_LOW (the
    steps are ~1 point), so SM-2 resets the interval and decays the ease by one
    penalty. 2.9 - 0.2 is the answer here; 2.3 would mean the DDL had supplied
    the starting value.
    """
    configured = settings.model_copy(update={"sm2_ease_initial": 2.9})

    async with client_for(configured) as client:
        headers = await authenticated(client)
        response = await attempt(client, seeded["drill"], headers=headers)

    assert response.status_code == 201
    stored = rows("SELECT ease_factor FROM concept_mastery")[0][0]
    assert stored == pytest.approx(2.9 - configured.sm2_ease_penalty)


async def test_a_second_attempt_moves_mastery_further(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID], rows: Rows
) -> None:
    headers = await authenticated(client)

    first = (await attempt(client, seeded["drill"], headers=headers)).json()
    second = (await attempt(client, seeded["drill"], headers=headers, audio=LONG_AUDIO)).json()

    assert second["mastery"]["previous"] == pytest.approx(first["mastery"]["current"])
    assert rows("SELECT attempt_count FROM concept_mastery") == [(2,)]
    assert rows("SELECT count(*) FROM attempts") == [(2,)]


async def test_a_failing_score_is_still_a_complete_attempt(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID], rows: Rows
) -> None:
    """Below the pass mark is a result, not an error: it costs an attempt,
    it is stored, and it pushes mastery down rather than up."""
    headers = await authenticated(client)

    body = (await attempt(client, seeded["failing_drill"], headers=headers)).json()

    assert body["passed"] is False
    assert body["mastery"]["current"] == 0.0  # clamped at the floor
    assert body["feedback"]["key"] == "attempt.feedback.retry"
    assert rows("SELECT passed FROM attempts") == [(False,)]
    assert rows("SELECT daily_attempts_used FROM entitlements") == [(1,)]


async def test_feedback_is_keys_and_content_not_a_sentence(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID]
) -> None:
    """CLAUDE.md section 7: user-visible copy never lives in Python."""
    headers = await authenticated(client)

    body = (await attempt(client, seeded["drill"], headers=headers)).json()

    assert body["feedback"]["key"] == "attempt.feedback.passed"
    assert body["feedback"]["km_explanation"] == "ការពន្យល់"


async def test_an_open_question_is_assessed_without_a_reference(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID], rows: Rows
) -> None:
    """Q&A has no target sentence, so completeness cannot be reported."""
    headers = await authenticated(client)

    body = (await attempt(client, seeded["qa"], headers=headers)).json()

    assert body["scores"]["completeness"] is None
    assert body["words"] == []
    assert rows("SELECT count(*) FROM attempts") == [(1,)]


async def test_an_item_without_a_concept_scores_but_advances_nothing(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID], rows: Rows
) -> None:
    headers = await authenticated(client)

    body = (await attempt(client, seeded["orphan_drill"], headers=headers)).json()

    assert body["mastery"] is None
    assert rows("SELECT count(*) FROM concept_mastery") == [(0,)]
    assert rows("SELECT count(*) FROM attempts") == [(1,)]


# --------------------------------------------------- the provider failure path


async def test_a_provider_failure_charges_nothing_and_stores_nothing(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID], rows: Rows
) -> None:
    """The D3 acceptance criterion. ARCHITECTURE section 5: our failure, not theirs.

    An empty recording makes FakeScorer return ok=False, which is the same
    shape a timed-out vendor produces.
    """
    headers = await authenticated(client)

    response = await attempt(client, seeded["drill"], headers=headers, audio=b"")

    assert response.status_code == 503
    assert response.json()["code"] == "scoring.unavailable"
    assert rows("SELECT count(*) FROM attempts") == [(0,)]
    assert rows("SELECT count(*) FROM concept_mastery") == [(0,)]
    assert rows("SELECT daily_attempts_used FROM entitlements") == [(0,)]


async def test_a_failed_call_is_still_recorded_as_a_call(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID], rows: Rows
) -> None:
    """Zero cost, but a row: the ledger records calls, not only charges."""
    headers = await authenticated(client)

    await attempt(client, seeded["drill"], headers=headers, audio=b"")

    assert rows("SELECT unit, cost_usd_cents_est FROM cost_ledger") == [("calls", 0)]


async def test_repeated_failures_do_not_drain_the_allowance(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID], rows: Rows
) -> None:
    """The release is idempotent enough to survive a learner retrying."""
    headers = await authenticated(client)

    for _ in range(3):
        await attempt(client, seeded["drill"], headers=headers, audio=b"")

    assert rows("SELECT daily_attempts_used FROM entitlements") == [(0,)]


# ------------------------------------------------------------------- refusals


async def test_the_daily_cap_refuses_before_anything_is_scored(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID], rows: Rows, execute: Execute
) -> None:
    """The cap is what holds PRD 11.2's cost ceiling, so it comes first."""
    headers = await authenticated(client)
    execute("UPDATE entitlements SET daily_attempts_used = 10")

    response = await attempt(client, seeded["drill"], headers=headers)

    assert response.status_code == 402
    assert response.json()["code"] == "quota.insufficient"
    assert rows("SELECT count(*) FROM cost_ledger") == [(0,)], "a refused learner was still scored"
    assert rows("SELECT count(*) FROM attempts") == [(0,)]


async def test_a_paid_plan_has_no_daily_cap(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID], execute: Execute, settings: Settings
) -> None:
    user_id = await sign_in(client, settings)
    execute(
        "INSERT INTO subscriptions (user_id, plan, status, period_start, period_end) "
        "VALUES (:u, 'basic', 'active', now(), now() + interval '30 days')",
        u=user_id,
    )
    execute("UPDATE entitlements SET daily_attempts_used = 99")
    headers = await authenticated(client)

    response = await attempt(client, seeded["drill"], headers=headers)

    assert response.status_code == 201
    assert response.json()["remaining_attempts"] is None


async def test_a_lecture_card_cannot_be_spoken_to(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID], rows: Rows
) -> None:
    headers = await authenticated(client)

    response = await attempt(client, seeded["explain"], headers=headers)

    assert response.status_code == 422
    assert response.json()["code"] == "attempt.item_not_scorable"
    assert rows("SELECT daily_attempts_used FROM entitlements") == [(0,)]


async def test_an_unknown_item_is_not_found(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID]
) -> None:
    headers = await authenticated(client)

    response = await attempt(client, uuid.uuid4(), headers=headers)

    assert response.status_code == 404


async def test_an_organisation_item_is_not_reachable(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID], rows: Rows
) -> None:
    """Same visibility rule as the catalogue (D-031), enforced on the write path."""
    headers = await authenticated(client)

    response = await attempt(client, seeded["org_drill"], headers=headers)

    assert response.status_code == 404
    assert rows("SELECT count(*) FROM attempts") == [(0,)]


async def test_an_oversized_recording_is_refused_before_it_is_scored(
    settings: Settings, seeded: dict[str, uuid.UUID], rows: Rows
) -> None:
    tiny_limit = settings.model_copy(update={"attempt_max_audio_bytes": 1024})

    async with client_for(tiny_limit) as client:
        headers = await authenticated(client)
        response = await attempt(client, seeded["drill"], headers=headers, audio=LONG_AUDIO)

    assert response.status_code == 413
    assert response.json()["code"] == "attempt.audio_too_large"
    assert rows("SELECT count(*) FROM cost_ledger") == [(0,)]
    assert rows("SELECT daily_attempts_used FROM entitlements") == [(0,)]


async def test_an_attempt_needs_a_token(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID]
) -> None:
    response = await attempt(client, seeded["drill"], headers={})

    assert response.status_code == 401


async def test_a_pack_with_no_target_sentence_fails_loudly(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID], rows: Rows
) -> None:
    """An invalid pack got imported. That is our defect, and it must not
    quietly degrade into an unscripted assessment nobody asked for."""
    headers = await authenticated(client)

    with pytest.raises(ValueError, match="target_text"):
        await attempt(client, seeded["broken_drill"], headers=headers)

    assert rows("SELECT daily_attempts_used FROM entitlements") == [(0,)]


# ---------------------------------------------------------------- daily reset


async def test_a_new_day_gives_the_allowance_back(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID], rows: Rows, execute: Execute
) -> None:
    """The reset runs on the request rather than from a worker: a reset that
    depends on a job running is a reset that silently does not happen."""
    headers = await authenticated(client)
    execute(
        "UPDATE entitlements SET daily_attempts_used = 10, reset_at = now() - interval '1 hour'"
    )

    response = await attempt(client, seeded["drill"], headers=headers)

    assert response.status_code == 201
    assert rows("SELECT daily_attempts_used FROM entitlements") == [(1,)]
    assert rows("SELECT reset_at > now() FROM entitlements") == [(True,)]


async def test_the_reset_does_not_fire_early(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID], rows: Rows, execute: Execute
) -> None:
    headers = await authenticated(client)
    execute(
        "UPDATE entitlements SET daily_attempts_used = 4, reset_at = now() + interval '5 hours'"
    )

    await attempt(client, seeded["drill"], headers=headers)

    assert rows("SELECT daily_attempts_used FROM entitlements") == [(5,)]


# ------------------------------------------------------------------ stability


async def test_the_same_recording_scores_the_same_twice(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID]
) -> None:
    """FakeScorer is a fixture, not a random number generator (CLAUDE.md 4)."""
    headers = await authenticated(client)

    first = (await attempt(client, seeded["drill"], headers=headers)).json()
    second = (await attempt(client, seeded["drill"], headers=headers)).json()

    assert first["scores"] == second["scores"]


async def test_two_learners_keep_separate_mastery(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID], rows: Rows
) -> None:
    one = await authenticated(client, telegram_id=111)
    two = await authenticated(client, telegram_id=222)

    await attempt(client, seeded["drill"], headers=one)
    await attempt(client, seeded["drill"], headers=two)
    await attempt(client, seeded["drill"], headers=two)

    assert rows("SELECT attempt_count FROM concept_mastery ORDER BY attempt_count") == [(1,), (2,)]
