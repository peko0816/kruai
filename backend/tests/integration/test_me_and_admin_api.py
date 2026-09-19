"""The two operational reads of BACKLOG D5.

    GET /api/v1/me/entitlements
    GET /api/v1/admin/costs

The acceptance is "the dashboard can see what D3 recorded", so several tests
here spend an allowance through the real attempts endpoint and then read it
back through these two — an aggregate over rows some other test inserted proves
the SQL, not the chain.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa

from app.core.config import Settings
from app.core.security import decode_access_token
from tests.integration.conftest import Execute, Rows, auth_header, client_for, token_for

pytestmark = pytest.mark.integration

ENTITLEMENTS_URL = "/api/v1/me/entitlements"
COSTS_URL = "/api/v1/admin/costs"
ATTEMPTS_URL = "/api/v1/attempts"

AUDIO = (
    Path(__file__).resolve().parents[1] / "fixtures" / "audio" / "drill_short.wav"
).read_bytes()

OPERATOR_TELEGRAM_ID = 90001
LEARNER_TELEGRAM_ID = 90002


@pytest.fixture
def drill_item(db: sa.Engine) -> uuid.UUID:
    """One scorable item, so a test can produce a real ledger row."""
    ids = {name: uuid.uuid4() for name in ("course", "lesson", "concept", "drill")}
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
                "VALUES (:id, 'exam', 'zh', 'HSK1', 'ភាសាចិន')"
            ),
            {"id": ids["course"]},
        )
        conn.execute(
            sa.text(
                "INSERT INTO lessons (id, course_id, concept_ids, sequence, title_km) "
                "VALUES (:id, :course, :concepts, 1, 'មេរៀន')"
            ),
            {"id": ids["lesson"], "course": ids["course"], "concepts": [ids["concept"]]},
        )
        conn.execute(
            sa.text(
                "INSERT INTO lesson_items (id, lesson_id, concept_id, item_type, payload, sequence) "
                "VALUES (:id, :lesson, :concept, 'drill', CAST('{\"target_text\": \"我要水\"}' AS jsonb), 1)"
            ),
            {"id": ids["drill"], "lesson": ids["lesson"], "concept": ids["concept"]},
        )
    return ids["drill"]


async def authenticated(client: httpx.AsyncClient, **kwargs: Any) -> dict[str, str]:
    return auth_header(await token_for(client, **kwargs))


async def speak(
    client: httpx.AsyncClient, item_id: uuid.UUID, *, headers: dict[str, str]
) -> httpx.Response:
    return await client.post(
        ATTEMPTS_URL,
        headers=headers,
        data={"lesson_item_id": str(item_id)},
        files={"audio": ("attempt.wav", AUDIO, "audio/wav")},
    )


def ledger(
    execute: Execute,
    *,
    provider: str,
    cost: int,
    user_id: uuid.UUID | None = None,
    days_ago: int = 0,
    unit: str = "calls",
    quantity: float = 1.0,
) -> None:
    """A cost_ledger row placed in the past, which no endpoint can do."""
    execute(
        "INSERT INTO cost_ledger "
        "(occurred_at, user_id, provider, unit, quantity, cost_usd_cents_est, ref) "
        "VALUES (now() - make_interval(days => :days), :u, :p, :unit, :q, :cost, 'attempt')",
        days=days_ago,
        u=user_id,
        p=provider,
        unit=unit,
        q=quantity,
        cost=cost,
    )


def with_operator(settings: Settings) -> Settings:
    return settings.model_copy(update={"admin_telegram_ids": (OPERATOR_TELEGRAM_ID,)})


# ------------------------------------------------------------- me/entitlements


async def test_a_new_learner_is_on_free_with_a_full_allowance(
    client: httpx.AsyncClient, settings: Settings
) -> None:
    headers = await authenticated(client)

    response = await client.get(ENTITLEMENTS_URL, headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["plan"] == "free"
    assert body["attempts"] == {
        "used": 0,
        "limit": settings.limit_free_daily_attempts,
        "remaining": settings.limit_free_daily_attempts,
    }
    assert body["tasks"]["limit"] == settings.limit_free_daily_tasks
    assert body["realtime_seconds_remaining"] == 0


async def test_speaking_shows_up_in_the_allowance(
    client: httpx.AsyncClient, drill_item: uuid.UUID, settings: Settings
) -> None:
    """The learner-facing half of the acceptance: what D3 spent is visible."""
    headers = await authenticated(client)

    await speak(client, drill_item, headers=headers)
    body = (await client.get(ENTITLEMENTS_URL, headers=headers)).json()

    assert body["attempts"]["used"] == 1
    assert body["attempts"]["remaining"] == settings.limit_free_daily_attempts - 1


async def test_a_paid_plan_reports_unlimited_as_null_not_zero(
    client: httpx.AsyncClient, execute: Execute, settings: Settings
) -> None:
    """0 is the internal sentinel for "no ceiling" (CONFIG_REFERENCE section 4).

    Sending it to a client is a paywall that reads "0 attempts left" to every
    paying subscriber.
    """
    user_id = decode_access_token(await token_for(client), settings=settings)
    execute(
        "INSERT INTO subscriptions (user_id, plan, status, period_start, period_end) "
        "VALUES (:u, 'basic', 'active', now(), now() + interval '30 days')",
        u=user_id,
    )
    headers = await authenticated(client)

    body = (await client.get(ENTITLEMENTS_URL, headers=headers)).json()

    assert body["plan"] == "basic"
    assert body["attempts"]["limit"] is None
    assert body["attempts"]["remaining"] is None


async def test_remaining_never_goes_negative(
    settings: Settings, execute: Execute, client: httpx.AsyncClient
) -> None:
    """A limit lowered mid-day leaves a counter above it.

    consume_attempt's guard stops a learner spending past the ceiling, but it
    cannot un-spend what they already had when the ceiling moves. "-89 attempts
    remaining" is not something an interface should have to draw.
    """
    await token_for(client)
    execute("UPDATE entitlements SET daily_attempts_used = 99")
    lowered = settings.model_copy(update={"limit_free_daily_attempts": 10})

    async with client_for(lowered) as tightened:
        headers = await authenticated(tightened)
        body = (await tightened.get(ENTITLEMENTS_URL, headers=headers)).json()

    assert body["attempts"]["used"] == 99
    assert body["attempts"]["remaining"] == 0


async def test_the_reset_time_is_reported_and_is_in_the_future(client: httpx.AsyncClient) -> None:
    headers = await authenticated(client)

    body = (await client.get(ENTITLEMENTS_URL, headers=headers)).json()

    resets_at = datetime.datetime.fromisoformat(body["resets_at"])
    assert resets_at > datetime.datetime.now(datetime.UTC)


async def test_reading_the_allowance_rolls_over_a_finished_day(
    client: httpx.AsyncClient, rows: Rows, execute: Execute
) -> None:
    """Otherwise a learner opening the app after their local midnight is shown
    yesterday's exhausted counters and believes they are still locked out."""
    headers = await authenticated(client)
    execute(
        "UPDATE entitlements SET daily_attempts_used = 99, daily_tasks_used = 3, "
        "reset_at = now() - interval '1 hour'"
    )

    body = (await client.get(ENTITLEMENTS_URL, headers=headers)).json()

    assert body["attempts"]["used"] == 0
    assert body["tasks"]["used"] == 0
    assert rows("SELECT daily_attempts_used FROM entitlements") == [(0,)]


async def test_reading_the_allowance_does_not_reset_early(
    client: httpx.AsyncClient, execute: Execute
) -> None:
    headers = await authenticated(client)
    execute(
        "UPDATE entitlements SET daily_attempts_used = 4, reset_at = now() + interval '5 hours'"
    )

    body = (await client.get(ENTITLEMENTS_URL, headers=headers)).json()

    assert body["attempts"]["used"] == 4


async def test_the_allowance_is_the_caller_s_own(
    client: httpx.AsyncClient, drill_item: uuid.UUID
) -> None:
    mine = await authenticated(client, telegram_id=5551)
    theirs = await authenticated(client, telegram_id=5552)
    await speak(client, drill_item, headers=mine)

    assert (await client.get(ENTITLEMENTS_URL, headers=mine)).json()["attempts"]["used"] == 1
    assert (await client.get(ENTITLEMENTS_URL, headers=theirs)).json()["attempts"]["used"] == 0


async def test_the_allowance_needs_a_token(client: httpx.AsyncClient) -> None:
    assert (await client.get(ENTITLEMENTS_URL)).status_code == 401


# -------------------------------------------------------------- admin/costs


async def test_the_dashboard_sees_what_an_attempt_recorded(
    settings: Settings, drill_item: uuid.UUID
) -> None:
    """The D5 acceptance, stated as a chain: speak, then read the money."""
    configured = with_operator(settings)

    async with client_for(configured) as client:
        learner = await authenticated(client, telegram_id=LEARNER_TELEGRAM_ID)
        await speak(client, drill_item, headers=learner)
        operator = await authenticated(client, telegram_id=OPERATOR_TELEGRAM_ID)

        body = (await client.get(COSTS_URL, headers=operator)).json()

    assert body["total_usd_cents"] == 1
    assert body["total_entries"] == 1
    assert body["rows"][0]["unit"] == "calls"
    assert body["rows"][0]["calls"] == 1


async def test_the_dashboard_reports_the_thresholds_spend_is_judged_against(
    settings: Settings,
) -> None:
    """So an operator reads a per-user total against the number that acts on
    it, rather than recomputing 1.5 times something in their head (D10)."""
    configured = with_operator(settings)

    async with client_for(configured) as client:
        headers = await authenticated(client, telegram_id=OPERATOR_TELEGRAM_ID)
        body = (await client.get(COSTS_URL, headers=headers)).json()

    assert body["alert_thresholds_usd_cents"] == {"free": 23, "basic": 68, "pro": 345}


async def test_grouping_by_provider(settings: Settings, execute: Execute) -> None:
    configured = with_operator(settings)

    async with client_for(configured) as client:
        operator_id = decode_access_token(
            await token_for(client, telegram_id=OPERATOR_TELEGRAM_ID), settings=configured
        )
        ledger(execute, provider="azure", cost=30, user_id=operator_id)
        ledger(execute, provider="azure", cost=12, user_id=operator_id)
        ledger(execute, provider="openai", cost=7, user_id=operator_id)
        headers = await authenticated(client, telegram_id=OPERATOR_TELEGRAM_ID)

        body = (await client.get(f"{COSTS_URL}?group_by=provider", headers=headers)).json()

    assert [(row["key"], row["cost_usd_cents"], row["calls"]) for row in body["rows"]] == [
        ("azure", 42, 2),
        ("openai", 7, 1),
    ]
    assert body["total_usd_cents"] == 49


async def test_grouping_by_user_finds_the_expensive_learner(
    settings: Settings, execute: Execute
) -> None:
    """PRD 11.2 caps spend per user per month, so this is the grouping that
    answers whether the cap is holding."""
    configured = with_operator(settings)

    async with client_for(configured) as client:
        cheap = decode_access_token(await token_for(client, telegram_id=1), settings=configured)
        pricey = decode_access_token(await token_for(client, telegram_id=2), settings=configured)
        ledger(execute, provider="azure", cost=5, user_id=cheap)
        ledger(execute, provider="azure", cost=200, user_id=pricey)
        headers = await authenticated(client, telegram_id=OPERATOR_TELEGRAM_ID)

        body = (await client.get(f"{COSTS_URL}?group_by=user", headers=headers)).json()

    assert body["rows"][0]["key"] == str(pricey)
    assert body["rows"][0]["cost_usd_cents"] == 200


async def test_a_call_with_no_user_is_reported_rather_than_dropped(
    settings: Settings, execute: Execute
) -> None:
    """Content production is billed to the pipeline, not to a learner (PRD 11.3),
    and it is still money."""
    configured = with_operator(settings)
    ledger(execute, provider="fake-tts", cost=88, user_id=None)

    async with client_for(configured) as client:
        headers = await authenticated(client, telegram_id=OPERATOR_TELEGRAM_ID)
        body = (await client.get(f"{COSTS_URL}?group_by=user", headers=headers)).json()

    assert body["rows"] == [
        {"key": None, "cost_usd_cents": 88, "unit": "calls", "quantity": 1.0, "calls": 1}
    ]


async def test_grouping_by_day(settings: Settings, execute: Execute) -> None:
    configured = with_operator(settings)
    ledger(execute, provider="azure", cost=10, days_ago=0)
    ledger(execute, provider="azure", cost=20, days_ago=1)
    ledger(execute, provider="azure", cost=3, days_ago=1)

    async with client_for(configured) as client:
        headers = await authenticated(client, telegram_id=OPERATOR_TELEGRAM_ID)
        body = (await client.get(f"{COSTS_URL}?group_by=day", headers=headers)).json()

    today = datetime.datetime.now(datetime.UTC).date()
    yesterday = today - datetime.timedelta(days=1)
    assert [(row["key"], row["cost_usd_cents"]) for row in body["rows"]] == [
        (yesterday.isoformat(), 23),
        (today.isoformat(), 10),
    ]


async def test_units_are_never_added_together(settings: Settings, execute: Execute) -> None:
    """Seconds plus tokens is a number that means nothing, and it would sit in
    the column next to a cost that is perfectly correct."""
    configured = with_operator(settings)
    ledger(execute, provider="azure", cost=10, unit="seconds", quantity=12.5)
    ledger(execute, provider="azure", cost=4, unit="tokens", quantity=900.0)

    async with client_for(configured) as client:
        headers = await authenticated(client, telegram_id=OPERATOR_TELEGRAM_ID)
        body = (await client.get(f"{COSTS_URL}?group_by=provider", headers=headers)).json()

    assert {(row["unit"], row["quantity"]) for row in body["rows"]} == {
        ("seconds", 12.5),
        ("tokens", 900.0),
    }
    assert body["total_usd_cents"] == 14


async def test_the_window_excludes_what_is_older(settings: Settings, execute: Execute) -> None:
    configured = with_operator(settings)
    ledger(execute, provider="azure", cost=5, days_ago=0)
    ledger(execute, provider="azure", cost=999, days_ago=90)

    async with client_for(configured) as client:
        headers = await authenticated(client, telegram_id=OPERATOR_TELEGRAM_ID)
        default_window = (await client.get(COSTS_URL, headers=headers)).json()
        wide = (
            await client.get(
                f"{COSTS_URL}?since={(datetime.date.today() - datetime.timedelta(days=120))}",
                headers=headers,
            )
        ).json()

    assert default_window["total_usd_cents"] == 5
    assert wide["total_usd_cents"] == 1004


async def test_a_row_late_on_the_final_day_is_included(
    settings: Settings, execute: Execute
) -> None:
    """``until`` names a day, not an instant. A call at 23:30 belongs to it."""
    configured = with_operator(settings)
    today = datetime.datetime.now(datetime.UTC).date()
    execute(
        "INSERT INTO cost_ledger (occurred_at, provider, unit, quantity, cost_usd_cents_est, ref) "
        "VALUES (:at, 'azure', 'calls', 1, 77, 'attempt')",
        at=datetime.datetime.combine(today, datetime.time(23, 30), tzinfo=datetime.UTC),
    )

    async with client_for(configured) as client:
        headers = await authenticated(client, telegram_id=OPERATOR_TELEGRAM_ID)
        body = (await client.get(f"{COSTS_URL}?until={today}", headers=headers)).json()

    assert body["total_usd_cents"] == 77


async def test_an_impossible_window_is_refused(settings: Settings) -> None:
    configured = with_operator(settings)

    async with client_for(configured) as client:
        headers = await authenticated(client, telegram_id=OPERATOR_TELEGRAM_ID)
        response = await client.get(
            f"{COSTS_URL}?since=2026-03-02&until=2026-03-01", headers=headers
        )

    assert response.status_code == 422
    assert response.json()["code"] == "query.window_invalid"


async def test_an_empty_ledger_reports_zero_rather_than_failing(
    settings: Settings,
) -> None:
    configured = with_operator(settings)

    async with client_for(configured) as client:
        headers = await authenticated(client, telegram_id=OPERATOR_TELEGRAM_ID)
        body = (await client.get(COSTS_URL, headers=headers)).json()

    assert body == {
        "group_by": "day",
        "since": body["since"],
        "until": body["until"],
        "alert_thresholds_usd_cents": {"free": 23, "basic": 68, "pro": 345},
        "total_usd_cents": 0,
        "total_entries": 0,
        "rows": [],
    }


# ------------------------------------------------------------------- access


async def test_nobody_can_read_the_dashboard_by_default(
    client: httpx.AsyncClient, execute: Execute
) -> None:
    """ADMIN_TELEGRAM_IDS ships empty. An ops endpoint that is open until
    someone remembers to close it is an open endpoint."""
    ledger(execute, provider="azure", cost=500)
    headers = await authenticated(client, telegram_id=OPERATOR_TELEGRAM_ID)

    response = await client.get(COSTS_URL, headers=headers)

    assert response.status_code == 403
    assert response.json()["code"] == "auth.forbidden"


async def test_an_unconfigured_deployment_says_so_in_the_log(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Both refusals are 403 to the caller and two different problems to an
    operator: "nobody is configured" and "you are not on the list" need
    different fixes, and the response deliberately cannot tell them apart.

    Asserted on the log rather than the body, because that asymmetry is the
    design — see the module docstring in core/security.py on why a refusal does
    not explain itself.
    """
    headers = await authenticated(client, telegram_id=OPERATOR_TELEGRAM_ID)

    with caplog.at_level(logging.WARNING):
        response = await client.get(COSTS_URL, headers=headers)

    assert response.status_code == 403
    assert "admin.no_operators_configured" in caplog.text


async def test_a_learner_cannot_read_other_people_s_spending(
    settings: Settings, execute: Execute
) -> None:
    configured = with_operator(settings)
    ledger(execute, provider="azure", cost=500)

    async with client_for(configured) as client:
        headers = await authenticated(client, telegram_id=LEARNER_TELEGRAM_ID)
        response = await client.get(COSTS_URL, headers=headers)

    assert response.status_code == 403
    assert "500" not in response.text


async def test_the_dashboard_needs_a_token(settings: Settings) -> None:
    configured = with_operator(settings)

    async with client_for(configured) as client:
        assert (await client.get(COSTS_URL)).status_code == 401


async def test_an_operator_is_recognised_by_their_telegram_id(
    settings: Settings, execute: Execute
) -> None:
    """Not by a claim in the token: privileges must not outlive the
    configuration that granted them (D-025)."""
    configured = with_operator(settings)
    ledger(execute, provider="azure", cost=11)

    async with client_for(configured) as client:
        headers = await authenticated(client, telegram_id=OPERATOR_TELEGRAM_ID)
        response = await client.get(COSTS_URL, headers=headers)

    assert response.status_code == 200
    assert response.json()["total_usd_cents"] == 11
