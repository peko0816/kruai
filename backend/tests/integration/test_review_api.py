"""Finishing a lesson, and what comes back to practise (BACKLOG D4).

    POST /api/v1/lessons/{id}/complete
    GET  /api/v1/review/queue

The acceptance is "mastery and the queue are correct after completing", and the
case that makes it worth testing is the concept a learner never spoke to. D3
only creates a concept_mastery row when an attempt lands, so a lesson clicked
through leaves part of itself untracked — done, and invisible to spaced
repetition forever.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.api.v1.review import DEFAULT_REVIEW_MINUTES, ReviewMinutes
from app.core.config import Settings
from app.core.security import decode_access_token
from app.services.mastery import REVIEW_DURATION_CHOICES
from tests.integration.conftest import Execute, Rows, auth_header, client_for, token_for

pytestmark = pytest.mark.integration

QUEUE_URL = "/api/v1/review/queue"
LESSONS_URL = "/api/v1/lessons"

#: Three concepts so an ordering assertion has something to order.
CONCEPTS = ("zh.hsk1.want_noun", "zh.hsk1.this_that", "zh.hsk1.how_many")


@pytest.fixture
def seeded(db: sa.Engine) -> dict[str, uuid.UUID]:
    """One visible lesson teaching three concepts, and one org lesson."""
    ids: dict[str, uuid.UUID] = {
        name: uuid.uuid4()
        for name in ("org", "course", "org_course", "lesson", "empty_lesson", "org_lesson")
    }
    for slug in CONCEPTS:
        ids[slug] = uuid.uuid4()

    with db.begin() as conn:
        conn.execute(
            sa.text("INSERT INTO orgs (id, name, seats) VALUES (:id, 'Hotel Co', 5)"),
            {"id": ids["org"]},
        )
        conn.execute(
            sa.text(
                "INSERT INTO concepts (id, slug, language, level, pattern, km_explanation, "
                "sort_order) VALUES (:id, :slug, 'zh', 'HSK1', :pattern, :km, :order)"
            ),
            [
                {
                    "id": ids[slug],
                    "slug": slug,
                    "pattern": f"pattern for {slug}",
                    "km": f"ការពន្យល់ {index}",
                    "order": index,
                }
                for index, slug in enumerate(CONCEPTS)
            ],
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
                "VALUES (:id, :course, :concepts, :sequence, 'មេរៀន')"
            ),
            [
                {
                    "id": ids["lesson"],
                    "course": ids["course"],
                    "concepts": [ids[slug] for slug in CONCEPTS],
                    "sequence": 1,
                },
                {
                    "id": ids["empty_lesson"],
                    "course": ids["course"],
                    "concepts": [],
                    "sequence": 2,
                },
                {
                    "id": ids["org_lesson"],
                    "course": ids["org_course"],
                    "concepts": [],
                    "sequence": 1,
                },
            ],
        )
    return ids


async def authenticated(client: httpx.AsyncClient, **kwargs: Any) -> dict[str, str]:
    return auth_header(await token_for(client, **kwargs))


async def complete(
    client: httpx.AsyncClient, lesson_id: uuid.UUID, *, headers: dict[str, str]
) -> httpx.Response:
    return await client.post(f"{LESSONS_URL}/{lesson_id}/complete", headers=headers)


def track(
    execute: Execute,
    user_id: uuid.UUID,
    concept_id: uuid.UUID,
    *,
    mastery: float,
    due_in_days: float,
    attempts: int = 1,
) -> None:
    """A concept_mastery row as D3 would have left it."""
    execute(
        "INSERT INTO concept_mastery "
        "(user_id, concept_id, mastery_score, attempt_count, ease_factor, interval_days, "
        " last_seen_at, next_due_at) "
        "VALUES (:u, :c, :m, :n, 2.5, 1, now(), now() + make_interval(secs => :secs))",
        u=user_id,
        c=concept_id,
        m=mastery,
        n=attempts,
        secs=due_in_days * 86400,
    )


# --------------------------------------------------------- starting a lesson


async def start(
    client: httpx.AsyncClient, lesson_id: uuid.UUID, *, headers: dict[str, str]
) -> httpx.Response:
    return await client.post(f"{LESSONS_URL}/{lesson_id}/start", headers=headers)


async def test_starting_a_lesson_spends_one_of_the_day_s_tasks(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID], rows: Rows, settings: Settings
) -> None:
    """PRD 4.3's Free allowance, enforced where it can still refuse something:
    at the door rather than on the way out (D-054)."""
    headers = await authenticated(client)

    response = await start(client, seeded["lesson"], headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["first_start"] is True
    assert body["remaining_tasks"] == settings.limit_free_daily_tasks - 1
    assert rows("SELECT daily_tasks_used FROM entitlements") == [(1,)]
    assert rows("SELECT status FROM lesson_progress") == [("started",)]


async def test_picking_a_lesson_back_up_is_free(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID], rows: Rows
) -> None:
    """One lesson over three sittings is one lesson. Charging three times would
    make the limit mean something nobody agreed to."""
    headers = await authenticated(client)

    await start(client, seeded["lesson"], headers=headers)
    again = await start(client, seeded["lesson"], headers=headers)

    assert again.json()["first_start"] is False
    assert rows("SELECT daily_tasks_used FROM entitlements") == [(1,)]


async def test_the_free_allowance_runs_out(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID], rows: Rows, execute: Execute
) -> None:
    headers = await authenticated(client)
    execute("UPDATE entitlements SET daily_tasks_used = 3")

    response = await start(client, seeded["lesson"], headers=headers)

    assert response.status_code == 402
    assert response.json()["code"] == "quota.insufficient"


async def test_a_refused_start_leaves_no_trace(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID], rows: Rows, execute: Execute
) -> None:
    """Otherwise the learner comes back tomorrow to find the lesson already
    marked started — and the task they were refused silently spent."""
    headers = await authenticated(client)
    execute("UPDATE entitlements SET daily_tasks_used = 3")

    await start(client, seeded["lesson"], headers=headers)

    assert rows("SELECT count(*) FROM lesson_progress") == [(0,)]
    assert rows("SELECT daily_tasks_used FROM entitlements") == [(3,)]


async def test_a_paid_plan_has_no_task_limit(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID], execute: Execute, settings: Settings
) -> None:
    user_id = await _user_id(client, settings)
    execute(
        "INSERT INTO subscriptions (user_id, plan, status, period_start, period_end) "
        "VALUES (:u, 'basic', 'active', now(), now() + interval '30 days')",
        u=user_id,
    )
    execute("UPDATE entitlements SET daily_tasks_used = 99")
    headers = await authenticated(client)

    response = await start(client, seeded["lesson"], headers=headers)

    assert response.status_code == 200
    assert response.json()["remaining_tasks"] is None


async def test_a_new_day_gives_the_tasks_back(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID], rows: Rows, execute: Execute
) -> None:
    headers = await authenticated(client)
    execute("UPDATE entitlements SET daily_tasks_used = 3, reset_at = now() - interval '1 hour'")

    response = await start(client, seeded["lesson"], headers=headers)

    assert response.status_code == 200
    assert rows("SELECT daily_tasks_used FROM entitlements") == [(1,)]


async def test_starting_an_organisation_lesson_is_not_found(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID], rows: Rows
) -> None:
    headers = await authenticated(client)

    response = await start(client, seeded["org_lesson"], headers=headers)

    assert response.status_code == 404
    assert rows("SELECT daily_tasks_used FROM entitlements") == [(0,)], "nothing was charged"


async def test_completing_a_started_lesson_does_not_charge_again(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID], rows: Rows
) -> None:
    """A task is one lesson, counted once, at the start."""
    headers = await authenticated(client)

    await start(client, seeded["lesson"], headers=headers)
    await complete(client, seeded["lesson"], headers=headers)

    assert rows("SELECT daily_tasks_used FROM entitlements") == [(1,)]
    assert rows("SELECT status FROM lesson_progress") == [("completed",)]


async def test_starting_needs_a_token(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID]
) -> None:
    assert (await start(client, seeded["lesson"], headers={})).status_code == 401


# ----------------------------------------------------------------- completion


async def test_completing_a_lesson_records_it(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID], rows: Rows
) -> None:
    headers = await authenticated(client)

    response = await complete(client, seeded["lesson"], headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"
    assert body["first_completion"] is True
    assert rows("SELECT status, completed_at IS NOT NULL FROM lesson_progress") == [
        ("completed", True)
    ]


async def test_completing_schedules_every_concept_the_learner_skipped(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID], rows: Rows, settings: Settings
) -> None:
    """The point of the endpoint. Without this the lesson is finished and two
    thirds of it never enters the review queue."""
    headers = await authenticated(client)

    body = (await complete(client, seeded["lesson"], headers=headers)).json()

    assert body["concepts_tracked"] == 3
    assert body["concepts_newly_scheduled"] == 3
    tracked = rows(
        "SELECT mastery_score, attempt_count, ease_factor, next_due_at > now() FROM concept_mastery"
    )
    assert len(tracked) == 3
    assert {row[0] for row in tracked} == {0.0}
    assert {row[1] for row in tracked} == {0}
    # L-6 again: seeded from SM2_EASE_INITIAL, not the column default.
    assert {round(row[2], 3) for row in tracked} == {round(settings.sm2_ease_initial, 3)}
    assert {row[3] for row in tracked} == {True}


async def test_seeded_rows_use_the_configured_ease_not_the_column_default(
    settings: Settings, seeded: dict[str, uuid.UUID], rows: Rows
) -> None:
    """The retired L-6, on the second writer of concept_mastery.

    The DDL default and SM2_EASE_INITIAL both say 2.5, so asserting 2.5 proves
    nothing about which one supplied it. Nothing decays here — completion
    schedules rather than scores — so the stored value is the configured
    initial exactly.
    """
    configured = settings.model_copy(update={"sm2_ease_initial": 2.9})

    async with client_for(configured) as client:
        headers = await authenticated(client)
        await complete(client, seeded["lesson"], headers=headers)

    assert {round(row[0], 3) for row in rows("SELECT ease_factor FROM concept_mastery")} == {2.9}


async def test_completing_does_not_disturb_a_concept_already_in_flight(
    client: httpx.AsyncClient,
    seeded: dict[str, uuid.UUID],
    rows: Rows,
    execute: Execute,
    settings: Settings,
) -> None:
    """A practised concept keeps its score, its ease and its due date.

    The stored row outranks any recompute — the same rule as the experiment
    assignments in D-023.
    """
    user_id = await _user_id(client, settings)
    headers = await authenticated(client)
    practised = seeded[CONCEPTS[0]]
    track(execute, user_id, practised, mastery=42.0, due_in_days=9, attempts=7)

    body = (await complete(client, seeded["lesson"], headers=headers)).json()

    assert body["concepts_newly_scheduled"] == 2
    kept = rows(
        "SELECT mastery_score, attempt_count FROM concept_mastery WHERE concept_id = :c",
        c=practised,
    )
    assert kept == [(42.0, 7)]


async def test_completing_twice_keeps_the_first_time(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID], rows: Rows
) -> None:
    """Revisiting a lesson has not un-finished it, and must not restamp it."""
    headers = await authenticated(client)

    first = (await complete(client, seeded["lesson"], headers=headers)).json()
    second = (await complete(client, seeded["lesson"], headers=headers)).json()

    assert first["first_completion"] is True
    assert second["first_completion"] is False
    assert second["completed_at"] == first["completed_at"]
    assert second["concepts_newly_scheduled"] == 0
    assert rows("SELECT count(*) FROM lesson_progress") == [(1,)]
    assert rows("SELECT count(*) FROM concept_mastery") == [(3,)]


async def test_a_lesson_with_no_concepts_completes_quietly(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID], rows: Rows
) -> None:
    headers = await authenticated(client)

    body = (await complete(client, seeded["empty_lesson"], headers=headers)).json()

    assert body["concepts_tracked"] == 0
    assert body["concepts_newly_scheduled"] == 0
    assert rows("SELECT count(*) FROM concept_mastery") == [(0,)]


async def test_two_learners_complete_independently(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID], rows: Rows
) -> None:
    one = await authenticated(client, telegram_id=4001)
    two = await authenticated(client, telegram_id=4002)

    await complete(client, seeded["lesson"], headers=one)

    assert rows("SELECT count(*) FROM lesson_progress") == [(1,)]
    assert rows("SELECT count(*) FROM concept_mastery") == [(3,)]

    await complete(client, seeded["lesson"], headers=two)

    assert rows("SELECT count(*) FROM lesson_progress") == [(2,)]
    assert rows("SELECT count(*) FROM concept_mastery") == [(6,)]


async def test_an_organisation_lesson_cannot_be_completed(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID], rows: Rows
) -> None:
    headers = await authenticated(client)

    response = await complete(client, seeded["org_lesson"], headers=headers)

    assert response.status_code == 404
    assert rows("SELECT count(*) FROM lesson_progress") == [(0,)]


async def test_an_unknown_lesson_cannot_be_completed(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID]
) -> None:
    headers = await authenticated(client)

    assert (await complete(client, uuid.uuid4(), headers=headers)).status_code == 404


async def test_completing_needs_a_token(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID]
) -> None:
    assert (await complete(client, seeded["lesson"], headers={})).status_code == 401


# ---------------------------------------------------------------- the queue


async def test_the_queue_is_empty_for_a_new_learner(client: httpx.AsyncClient) -> None:
    headers = await authenticated(client)

    response = await client.get(QUEUE_URL, headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["entries"] == []
    assert body["skipped"] == 0
    assert body["requested_minutes"] == DEFAULT_REVIEW_MINUTES.value


async def test_a_completed_lesson_fills_the_queue(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID], execute: Execute
) -> None:
    """The D4 acceptance, end to end: finish a lesson, get it back to review.

    Completion schedules the concepts for tomorrow, so nothing is due yet —
    pulling the due date back is what a day passing would do.
    """
    headers = await authenticated(client)
    await complete(client, seeded["lesson"], headers=headers)

    before = (await client.get(QUEUE_URL, headers=headers)).json()
    execute("UPDATE concept_mastery SET next_due_at = now() - interval '1 minute'")
    after = (await client.get(QUEUE_URL, headers=headers)).json()

    assert before["entries"] == [], "a concept scheduled for tomorrow is not due today"
    assert len(after["entries"]) == 3
    assert {entry["slug"] for entry in after["entries"]} == set(CONCEPTS)


async def test_the_queue_carries_what_a_client_needs_to_teach_the_concept(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID], execute: Execute, settings: Settings
) -> None:
    user_id = await _user_id(client, settings)
    headers = await authenticated(client)
    track(execute, user_id, seeded[CONCEPTS[0]], mastery=30.0, due_in_days=-1, attempts=4)

    entry = (await client.get(QUEUE_URL, headers=headers)).json()["entries"][0]

    assert entry["slug"] == CONCEPTS[0]
    assert entry["pattern"] == f"pattern for {CONCEPTS[0]}"
    assert entry["km_explanation"] == "ការពន្យល់ 0"
    assert entry["level"] == "HSK1"
    assert entry["mastery"] == 30.0
    assert entry["attempt_count"] == 4
    assert entry["estimated_seconds"] == settings.review_estimated_seconds_per_concept


async def test_the_weakest_concept_comes_first(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID], execute: Execute, settings: Settings
) -> None:
    """PRD 3.2: weakest first, because attention is the scarce resource."""
    user_id = await _user_id(client, settings)
    headers = await authenticated(client)
    track(execute, user_id, seeded[CONCEPTS[0]], mastery=80.0, due_in_days=-1)
    track(execute, user_id, seeded[CONCEPTS[1]], mastery=10.0, due_in_days=-1)
    track(execute, user_id, seeded[CONCEPTS[2]], mastery=45.0, due_in_days=-1)

    body = (await client.get(QUEUE_URL, headers=headers)).json()

    assert [entry["slug"] for entry in body["entries"]] == [
        CONCEPTS[1],
        CONCEPTS[2],
        CONCEPTS[0],
    ]


async def test_a_concept_that_is_not_due_stays_out(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID], execute: Execute, settings: Settings
) -> None:
    """Weakest-first over everything would resurface a concept practised five
    minutes ago, which is spaced repetition switched off."""
    user_id = await _user_id(client, settings)
    headers = await authenticated(client)
    track(execute, user_id, seeded[CONCEPTS[0]], mastery=1.0, due_in_days=3)
    track(execute, user_id, seeded[CONCEPTS[1]], mastery=70.0, due_in_days=-1)

    body = (await client.get(QUEUE_URL, headers=headers)).json()

    assert [entry["slug"] for entry in body["entries"]] == [CONCEPTS[1]]


async def test_the_slot_limits_how_much_comes_back(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID], execute: Execute, settings: Settings
) -> None:
    """Five minutes at sixty seconds a concept fits five; three are due."""
    user_id = await _user_id(client, settings)
    headers = await authenticated(client)
    for index, slug in enumerate(CONCEPTS):
        track(execute, user_id, seeded[slug], mastery=float(index), due_in_days=-1)

    full = (await client.get(f"{QUEUE_URL}?minutes=10", headers=headers)).json()
    sliced = (await client.get(f"{QUEUE_URL}?minutes=5", headers=headers)).json()

    assert len(full["entries"]) == 3
    assert full["skipped"] == 0
    assert len(sliced["entries"]) == 3, "three minutes of work fits a five minute slot"
    assert sliced["estimated_seconds"] == 3 * settings.review_estimated_seconds_per_concept


async def test_what_does_not_fit_is_counted_rather_than_hidden(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID], execute: Execute, settings: Settings
) -> None:
    """A client can then say "one more waiting" instead of "you are finished"."""
    long_reviews = settings.model_copy(update={"review_estimated_seconds_per_concept": 120})

    async with client_for(long_reviews) as client_with_long_reviews:
        user_id = await _user_id(client_with_long_reviews, long_reviews)
        headers = await authenticated(client_with_long_reviews)
        for index, slug in enumerate(CONCEPTS):
            track(execute, user_id, seeded[slug], mastery=float(index), due_in_days=-1)

        body = (
            await client_with_long_reviews.get(f"{QUEUE_URL}?minutes=5", headers=headers)
        ).json()

    assert len(body["entries"]) == 2
    assert body["skipped"] == 1
    assert body["over_budget"] is False


async def test_one_oversized_concept_is_still_returned(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID], execute: Execute, settings: Settings
) -> None:
    """D-017: the budget is a target. Telling a learner with overdue work that
    there is nothing to review is the worse answer."""
    huge = settings.model_copy(update={"review_estimated_seconds_per_concept": 900})

    async with client_for(huge) as client_with_huge_reviews:
        user_id = await _user_id(client_with_huge_reviews, huge)
        headers = await authenticated(client_with_huge_reviews)
        track(execute, user_id, seeded[CONCEPTS[0]], mastery=5.0, due_in_days=-1)

        body = (
            await client_with_huge_reviews.get(f"{QUEUE_URL}?minutes=5", headers=headers)
        ).json()

    assert len(body["entries"]) == 1
    assert body["over_budget"] is True


async def test_one_learners_queue_is_their_own(
    client: httpx.AsyncClient, seeded: dict[str, uuid.UUID], execute: Execute, settings: Settings
) -> None:
    mine = decode_access_token(await token_for(client, telegram_id=7001), settings=settings)
    my_headers = auth_header(await token_for(client, telegram_id=7001))
    their_headers = auth_header(await token_for(client, telegram_id=7002))
    track(execute, mine, seeded[CONCEPTS[0]], mastery=5.0, due_in_days=-1)

    mine_queue = (await client.get(QUEUE_URL, headers=my_headers)).json()
    theirs = (await client.get(QUEUE_URL, headers=their_headers)).json()

    assert [entry["slug"] for entry in mine_queue["entries"]] == [CONCEPTS[0]]
    assert theirs["entries"] == []


@pytest.mark.parametrize("minutes", [3, 0, -5, 30, 1000])
async def test_a_slot_the_interface_does_not_offer_is_refused(
    client: httpx.AsyncClient, minutes: int
) -> None:
    headers = await authenticated(client)

    response = await client.get(f"{QUEUE_URL}?minutes={minutes}", headers=headers)

    assert response.status_code == 422


@pytest.mark.parametrize("minutes", REVIEW_DURATION_CHOICES)
async def test_every_offered_slot_is_accepted(client: httpx.AsyncClient, minutes: int) -> None:
    headers = await authenticated(client)

    response = await client.get(f"{QUEUE_URL}?minutes={minutes}", headers=headers)

    assert response.status_code == 200
    assert response.json()["requested_minutes"] == minutes


async def test_the_queue_needs_a_token(client: httpx.AsyncClient) -> None:
    assert (await client.get(QUEUE_URL)).status_code == 401


async def test_a_practised_concept_cannot_be_deleted_from_under_the_queue(
    seeded: dict[str, uuid.UUID], execute: Execute, client: httpx.AsyncClient, settings: Settings
) -> None:
    """Written to prove the queue survives a concept vanishing; it proved the
    concept cannot vanish.

    concept_mastery.concept_id is a foreign key with no ON DELETE, so the
    database refuses. That is why the queue's join cannot drop an entry, and
    why the docstring that claimed it was guarding against this was wrong.
    """
    user_id = await _user_id(client, settings)
    track(execute, user_id, seeded[CONCEPTS[0]], mastery=5.0, due_in_days=-1)

    with pytest.raises(IntegrityError):
        execute("DELETE FROM concepts WHERE id = :c", c=seeded[CONCEPTS[0]])


# ------------------------------------------------------------------- helpers


async def _user_id(client: httpx.AsyncClient, settings: Settings) -> uuid.UUID:
    """The id behind the default sign-in, for seeding rows the API will read."""
    return decode_access_token(await token_for(client), settings=settings)


def test_the_query_parameter_offers_exactly_the_domain_choices() -> None:
    """The enum restates REVIEW_DURATION_CHOICES; this keeps them equal."""
    assert {member.value for member in ReviewMinutes} == set(REVIEW_DURATION_CHOICES)
    assert DEFAULT_REVIEW_MINUTES.value in REVIEW_DURATION_CHOICES
