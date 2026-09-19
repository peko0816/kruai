"""The three read endpoints of PRD section 10, against a real database.

The interesting half is the last one. ``GET /lessons/{id}`` has to return both
URLs *and* an instruction, because ARCHITECTURE 2.2 puts the choice on the
server and PRD 7.3 makes the audio fallback mandatory — so several tests here
assert that audio is still reachable in the responses where video won.

Which learner sees which arm is not read from a request header anywhere: plan
comes from subscriptions, data saver from the profile, the arm from the
experiments table. A test that set them via the client would be testing a
client that does not exist.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import sqlalchemy as sa

from app.core.config import Settings
from app.services.media import AVATAR_DISCLAIMER_KEY
from tests.integration.conftest import Execute, auth_header, client_for, sign_in, token_for

pytestmark = pytest.mark.integration

COURSES_URL = "/api/v1/courses"
LESSONS_URL = "/api/v1/lessons"

AUDIO_URL = "https://cdn.example/hsk1-explain-001.mp3"
VIDEO_URL = "https://cdn.example/hsk1-explain-001.mp4"
DRILL_AUDIO_URL = "https://cdn.example/hsk1-drill-001.mp3"


@dataclass(frozen=True)
class Content:
    """Ids of the rows seeded for these tests."""

    org_id: uuid.UUID
    course_id: uuid.UUID
    other_language_course_id: uuid.UUID
    org_course_id: uuid.UUID
    lesson_id: uuid.UUID
    second_lesson_id: uuid.UUID
    org_lesson_id: uuid.UUID
    concept_id: uuid.UUID
    explain_item_id: uuid.UUID
    drill_item_id: uuid.UUID
    silent_item_id: uuid.UUID


@pytest.fixture
def content(db: sa.Engine) -> Content:
    """One HSK1 course a learner can see, one job course they cannot.

    The lesson carries all three media shapes on purpose: an item with both
    recordings, one with audio only, and one with nothing at all — the
    media_pending case build_pack.py produces when TTS fails (ARCHITECTURE
    section 5), which must degrade to text rather than break the lesson.
    """
    ids: dict[str, Any] = {name: uuid.uuid4() for name in Content.__dataclass_fields__}
    seeded = Content(**ids)

    with db.begin() as conn:
        conn.execute(
            sa.text("INSERT INTO orgs (id, name, seats) VALUES (:id, 'Hotel Co', 20)"),
            {"id": seeded.org_id},
        )
        conn.execute(
            sa.text(
                "INSERT INTO concepts (id, slug, language, level, pattern, km_explanation) "
                "VALUES (:id, 'zh.hsk1.want_noun', 'zh', 'HSK1', '我要 + [名词]', 'ខ្ញុំចង់បាន')"
            ),
            {"id": seeded.concept_id},
        )
        conn.execute(
            sa.text(
                "INSERT INTO courses (id, course_type, language, level, title_km, title_zh, org_id) "
                "VALUES (:id, :type, :language, :level, :km, :zh, :org)"
            ),
            [
                {
                    "id": seeded.course_id,
                    "type": "exam",
                    "language": "zh",
                    "level": "HSK1",
                    "km": "ភាសាចិន HSK1",
                    "zh": "HSK1 中文",
                    "org": None,
                },
                {
                    "id": seeded.other_language_course_id,
                    "type": "exam",
                    "language": "en",
                    "level": "A1",
                    "km": "ភាសាអង់គ្លេស A1",
                    "zh": None,
                    "org": None,
                },
                {
                    "id": seeded.org_course_id,
                    "type": "job",
                    "language": "zh",
                    "level": "HOTEL",
                    "km": "ភាសាចិនសណ្ឋាគារ",
                    "zh": "酒店前台中文",
                    "org": seeded.org_id,
                },
            ],
        )
        conn.execute(
            sa.text(
                "INSERT INTO lessons (id, course_id, concept_ids, sequence, title_km) "
                "VALUES (:id, :course, :concepts, :sequence, :km)"
            ),
            [
                {
                    "id": seeded.lesson_id,
                    "course": seeded.course_id,
                    "concepts": [seeded.concept_id],
                    "sequence": 1,
                    "km": "មេរៀនទី ១",
                },
                {
                    "id": seeded.second_lesson_id,
                    "course": seeded.course_id,
                    "concepts": [seeded.concept_id],
                    "sequence": 2,
                    "km": "មេរៀនទី ២",
                },
                {
                    "id": seeded.org_lesson_id,
                    "course": seeded.org_course_id,
                    "concepts": [],
                    "sequence": 1,
                    "km": "មេរៀនការងារ",
                },
            ],
        )
        conn.execute(
            sa.text(
                "INSERT INTO lesson_items "
                "(id, lesson_id, concept_id, item_type, payload, media_type, anchor, sequence) "
                "VALUES (:id, :lesson, :concept, :type, CAST(:payload AS jsonb), :media, :anchor, :sequence)"
            ),
            [
                {
                    "id": seeded.explain_item_id,
                    "lesson": seeded.lesson_id,
                    "concept": seeded.concept_id,
                    "type": "explain",
                    "payload": '{"km": "ការពន្យល់", "zh": "我要水"}',
                    "media": "video",
                    "anchor": True,
                    "sequence": 1,
                },
                {
                    "id": seeded.drill_item_id,
                    "lesson": seeded.lesson_id,
                    "concept": seeded.concept_id,
                    "type": "drill",
                    "payload": '{"target": "我要水"}',
                    "media": "audio",
                    "anchor": False,
                    "sequence": 2,
                },
                {
                    "id": seeded.silent_item_id,
                    "lesson": seeded.lesson_id,
                    "concept": None,
                    "type": "vocab",
                    "payload": '{"word": "水"}',
                    "media": "audio",
                    "anchor": False,
                    "sequence": 3,
                },
            ],
        )
        conn.execute(
            sa.text(
                "INSERT INTO media_assets "
                "(lesson_item_id, kind, url, duration_ms, provider, source_text_hash) "
                "VALUES (:item, :kind, :url, 1200, 'fake', 'hash')"
            ),
            [
                {"item": seeded.explain_item_id, "kind": "audio", "url": AUDIO_URL},
                {"item": seeded.explain_item_id, "kind": "video", "url": VIDEO_URL},
                {"item": seeded.drill_item_id, "kind": "audio", "url": DRILL_AUDIO_URL},
            ],
        )
    return seeded


async def authenticated(client: httpx.AsyncClient, **kwargs: Any) -> dict[str, str]:
    return auth_header(await token_for(client, **kwargs))


async def opened(
    client: httpx.AsyncClient, lesson_id: uuid.UUID, *, headers: dict[str, str]
) -> None:
    """Start a lesson, because reading its contents now requires it.

    The contents are the lesson, so handing them over to somebody who never
    started it made the daily task allowance something clients enforced on
    themselves (D-058). Every test that reads a lesson goes through the door.
    """
    response = await client.post(f"{LESSONS_URL}/{lesson_id}/start", headers=headers)
    assert response.status_code == 200, response.text


def subscribe(execute: Execute, user_id: uuid.UUID, *, plan: str, status: str = "active") -> None:
    """A live subscription, as D8a will write one after a payment."""
    execute(
        "INSERT INTO subscriptions (user_id, plan, status, period_start, period_end) "
        "VALUES (:u, :plan, :status, now() - interval '1 day', now() + interval '29 days')",
        u=user_id,
        plan=plan,
        status=status,
    )


# ------------------------------------------------------------------ the catalogue


async def test_the_catalogue_lists_courses(client: httpx.AsyncClient, content: Content) -> None:
    headers = await authenticated(client)

    response = await client.get(COURSES_URL, headers=headers)

    assert response.status_code == 200
    listed = {row["id"]: row for row in response.json()}
    assert str(content.course_id) in listed
    assert listed[str(content.course_id)]["title_km"] == "ភាសាចិន HSK1"
    assert listed[str(content.course_id)]["level"] == "HSK1"


async def test_an_organisation_course_is_not_in_the_consumer_catalogue(
    client: httpx.AsyncClient, content: Content
) -> None:
    """Membership lands in M4; until then the safe answer is to serve none."""
    headers = await authenticated(client)

    response = await client.get(COURSES_URL, headers=headers)

    assert str(content.org_course_id) not in {row["id"] for row in response.json()}


@pytest.mark.parametrize(
    ("query", "expected_field", "expected_value"),
    [
        ("?language=zh", "language", "zh"),
        ("?language=en", "language", "en"),
        ("?type=exam", "course_type", "exam"),
    ],
)
async def test_the_catalogue_can_be_narrowed(
    client: httpx.AsyncClient,
    content: Content,
    query: str,
    expected_field: str,
    expected_value: str,
) -> None:
    headers = await authenticated(client)

    body = (await client.get(f"{COURSES_URL}{query}", headers=headers)).json()

    assert body, f"{query} matched nothing"
    assert {row[expected_field] for row in body} == {expected_value}


async def test_a_filter_matching_nothing_is_an_empty_list(
    client: httpx.AsyncClient, content: Content
) -> None:
    headers = await authenticated(client)

    response = await client.get(f"{COURSES_URL}?language=km", headers=headers)

    assert response.status_code == 200
    assert response.json() == []


async def test_lessons_come_back_in_teaching_order(
    client: httpx.AsyncClient, content: Content
) -> None:
    headers = await authenticated(client)

    response = await client.get(f"{COURSES_URL}/{content.course_id}/lessons", headers=headers)

    assert response.status_code == 200
    body = response.json()
    lessons = body["lessons"]
    assert [row["sequence"] for row in lessons] == [1, 2]
    assert [row["id"] for row in lessons] == [str(content.lesson_id), str(content.second_lesson_id)]
    assert lessons[0]["concept_ids"] == [str(content.concept_id)]
    assert [row["status"] for row in lessons] == ["not_started", "not_started"]
    assert body["next_lesson_id"] == str(content.lesson_id)


async def test_an_unknown_course_is_not_an_empty_lesson_list(
    client: httpx.AsyncClient, content: Content
) -> None:
    """Empty would claim the course exists. A client can act on the difference."""
    headers = await authenticated(client)

    response = await client.get(f"{COURSES_URL}/{uuid.uuid4()}/lessons", headers=headers)

    assert response.status_code == 404
    assert response.json()["code"] == "content.not_found"


async def test_an_organisation_course_is_not_readable_by_id(
    client: httpx.AsyncClient, content: Content
) -> None:
    """Same answer as "no such course": the id space is not a directory."""
    headers = await authenticated(client)

    response = await client.get(f"{COURSES_URL}/{content.org_course_id}/lessons", headers=headers)

    assert response.status_code == 404


# ---------------------------------------------------------------- authentication


@pytest.mark.parametrize(
    "path",
    ["/api/v1/courses", "/api/v1/courses/{course}/lessons", "/api/v1/lessons/{lesson}"],
)
async def test_every_read_needs_a_token(
    client: httpx.AsyncClient, content: Content, path: str
) -> None:
    url = path.format(course=content.course_id, lesson=content.lesson_id)

    response = await client.get(url)

    assert response.status_code == 401
    assert response.json()["code"] == "auth.invalid"


@pytest.mark.parametrize(
    "header",
    [
        {"Authorization": "Bearer not-a-token"},
        {"Authorization": "Basic abc"},
        {"Authorization": ""},
    ],
)
async def test_a_bad_authorization_header_is_refused(
    client: httpx.AsyncClient, content: Content, header: dict[str, str]
) -> None:
    response = await client.get(COURSES_URL, headers=header)

    assert response.status_code == 401


# ------------------------------------------------------------------- one lesson


async def test_a_lesson_returns_its_items_in_order(
    client: httpx.AsyncClient, content: Content
) -> None:
    headers = await authenticated(client)
    await opened(client, content.lesson_id, headers=headers)

    response = await client.get(f"{LESSONS_URL}/{content.lesson_id}", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["title_km"] == "មេរៀនទី ១"
    assert [item["sequence"] for item in body["items"]] == [1, 2, 3]
    assert [item["item_type"] for item in body["items"]] == ["explain", "drill", "vocab"]


async def test_the_content_payload_is_passed_through_unchanged(
    client: httpx.AsyncClient, content: Content
) -> None:
    """The pack's JSON is content, not something the API reshapes."""
    headers = await authenticated(client)
    await opened(client, content.lesson_id, headers=headers)

    body = (await client.get(f"{LESSONS_URL}/{content.lesson_id}", headers=headers)).json()

    assert body["items"][0]["payload"] == {"km": "ការពន្យល់", "zh": "我要水"}
    assert body["items"][0]["concept_id"] == str(content.concept_id)
    assert body["items"][2]["concept_id"] is None


async def test_an_unknown_lesson_is_not_found(client: httpx.AsyncClient, content: Content) -> None:
    headers = await authenticated(client)

    response = await client.get(f"{LESSONS_URL}/{uuid.uuid4()}", headers=headers)

    assert response.status_code == 404


async def test_an_organisation_lesson_is_not_reachable_by_id(
    client: httpx.AsyncClient, content: Content
) -> None:
    """Visibility is inherited from the course, so a leaked lesson id is not a way in."""
    headers = await authenticated(client)

    response = await client.get(f"{LESSONS_URL}/{content.org_lesson_id}", headers=headers)

    assert response.status_code == 404


# ------------------------------------------------------------- media resolution


async def test_both_urls_are_returned_and_the_server_picks(
    client: httpx.AsyncClient, settings: Settings, content: Content
) -> None:
    """The BACKLOG D2 acceptance criterion, stated as a response.

    MEDIA_VIDEO_ENABLED is false by default (PRD 7.2), so audio is what plays —
    but the video URL is still reported, because it is a fact about the content
    and the client is never the one deciding.
    """
    headers = await authenticated(client)
    await opened(client, content.lesson_id, headers=headers)

    body = (await client.get(f"{LESSONS_URL}/{content.lesson_id}", headers=headers)).json()
    explain = body["items"][0]["media"]

    assert explain["audio_url"] == AUDIO_URL
    assert explain["video_url"] == VIDEO_URL
    assert explain["primary_kind"] == "audio"
    assert explain["primary_url"] == AUDIO_URL
    assert explain["decision"] == "video_globally_disabled"
    assert explain["client_timeout_ms"] == settings.media_client_video_timeout_ms
    assert explain["disclaimer_key"] is None


async def test_an_item_with_no_recording_degrades_to_text(
    client: httpx.AsyncClient, content: Content
) -> None:
    """build_pack.py ships a pack whose TTS failed; the lesson still opens."""
    headers = await authenticated(client)
    await opened(client, content.lesson_id, headers=headers)

    body = (await client.get(f"{LESSONS_URL}/{content.lesson_id}", headers=headers)).json()
    silent = body["items"][2]["media"]

    assert silent["primary_kind"] is None
    assert silent["primary_url"] is None
    assert (silent["audio_url"], silent["video_url"]) == (None, None)
    assert silent["decision"] == "text_only"


async def test_video_is_served_with_audio_underneath_it(
    settings: Settings, content: Content, execute: Execute
) -> None:
    """PRD 7.3: the fallback is the availability floor, not a nicety."""
    video_on = settings.model_copy(update={"media_video_enabled": True})

    async with client_for(video_on) as client:
        user_id = await sign_in(client, video_on)
        subscribe(execute, user_id, plan="pro")
        headers = await authenticated(client)
        await opened(client, content.lesson_id, headers=headers)

        body = (await client.get(f"{LESSONS_URL}/{content.lesson_id}", headers=headers)).json()

    explain = body["items"][0]["media"]
    assert explain["primary_kind"] == "video"
    assert explain["primary_url"] == VIDEO_URL
    assert explain["fallback_url"] == AUDIO_URL
    assert explain["decision"] == "video"
    # PRD 7.4: the avatar is labelled wherever it appears.
    assert explain["disclaimer_key"] == AVATAR_DISCLAIMER_KEY


async def test_an_audio_only_item_has_no_fallback_to_offer(
    settings: Settings, content: Content, execute: Execute
) -> None:
    video_on = settings.model_copy(update={"media_video_enabled": True})

    async with client_for(video_on) as client:
        user_id = await sign_in(client, video_on)
        subscribe(execute, user_id, plan="pro")
        headers = await authenticated(client)
        await opened(client, content.lesson_id, headers=headers)

        body = (await client.get(f"{LESSONS_URL}/{content.lesson_id}", headers=headers)).json()

    drill = body["items"][1]["media"]
    assert drill["primary_kind"] == "audio"
    assert drill["fallback_url"] is None
    assert drill["decision"] == "item_is_audio_only"


async def test_data_saver_outranks_a_paid_plan(
    settings: Settings, content: Content, execute: Execute
) -> None:
    """A deliberate choice about someone's data bill beats their entitlement."""
    video_on = settings.model_copy(update={"media_video_enabled": True})

    async with client_for(video_on) as client:
        user_id = await sign_in(client, video_on)
        subscribe(execute, user_id, plan="pro")
        execute("UPDATE user_profiles SET data_saver = true WHERE user_id = :u", u=user_id)
        headers = await authenticated(client)
        await opened(client, content.lesson_id, headers=headers)

        body = (await client.get(f"{LESSONS_URL}/{content.lesson_id}", headers=headers)).json()

    assert body["items"][0]["media"]["decision"] == "data_saver"
    assert body["items"][0]["media"]["primary_kind"] == "audio"


@pytest.mark.parametrize(
    ("plan", "status", "expected"),
    [
        ("pro", "active", "video"),
        ("pro", "grace", "video"),
        ("basic", "active", "plan_below_minimum"),
        ("pro", "expired", "plan_below_minimum"),
        ("pro", "cancelled", "plan_below_minimum"),
    ],
)
async def test_the_plan_comes_from_the_subscription_row(
    settings: Settings,
    content: Content,
    execute: Execute,
    plan: str,
    status: str,
    expected: str,
) -> None:
    """Not from the token, and not from anything the client can send.

    Grace entitles (ARCHITECTURE 3.4): a payment a day late does not take the
    lesson away mid-session.
    """
    video_on = settings.model_copy(update={"media_video_enabled": True})

    async with client_for(video_on) as client:
        user_id = await sign_in(client, video_on)
        subscribe(execute, user_id, plan=plan, status=status)
        headers = await authenticated(client)
        await opened(client, content.lesson_id, headers=headers)

        body = (await client.get(f"{LESSONS_URL}/{content.lesson_id}", headers=headers)).json()

    assert body["items"][0]["media"]["decision"] == expected


async def test_a_learner_with_no_subscription_is_on_free(
    settings: Settings, content: Content
) -> None:
    video_on = settings.model_copy(update={"media_video_enabled": True})

    async with client_for(video_on) as client:
        headers = await authenticated(client)
        await opened(client, content.lesson_id, headers=headers)
        body = (await client.get(f"{LESSONS_URL}/{content.lesson_id}", headers=headers)).json()

    assert body["items"][0]["media"]["decision"] == "plan_below_minimum"


async def test_the_experiment_assigns_the_arm_when_it_is_running(
    settings: Settings, content: Content, execute: Execute, rows: Any
) -> None:
    """The lesson read is where a learner is actually exposed to the difference.

    Split 0 puts everybody in the audio arm, so the outcome is knowable without
    reproducing the hash here — bucket.py's tests own that.
    """
    running = settings.model_copy(
        update={
            "media_video_enabled": True,
            "experiment_explain_media_enabled": True,
            "experiment_explain_media_split": 0.0,
        }
    )

    async with client_for(running) as client:
        user_id = await sign_in(client, running)
        subscribe(execute, user_id, plan="pro")
        headers = await authenticated(client)
        await opened(client, content.lesson_id, headers=headers)

        body = (await client.get(f"{LESSONS_URL}/{content.lesson_id}", headers=headers)).json()

    assert body["items"][0]["media"]["decision"] == "experiment_control"
    assert rows("SELECT experiment_key, variant FROM experiments") == [("explain_media", "audio")]


async def test_nothing_is_assigned_while_the_experiment_is_off(
    client: httpx.AsyncClient, content: Content, rows: Any
) -> None:
    """D-022: a row written while the experiment is off is a phantom exposure."""
    headers = await authenticated(client)
    await opened(client, content.lesson_id, headers=headers)

    await client.get(f"{LESSONS_URL}/{content.lesson_id}", headers=headers)

    assert rows("SELECT count(*) FROM experiments") == [(0,)]
