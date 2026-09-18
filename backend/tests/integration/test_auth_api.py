"""POST /api/v1/auth/telegram against a real database.

The unit tests decide what counts as a valid signature. These decide what the
endpoint does with the answer: which rows appear, what a second sign-in does to
them, and what a forgery leaves behind — which must be nothing.

Rows are read over a separate synchronous connection rather than through the
application's session, so an assertion sees what was committed rather than what
some open transaction is holding.
"""

from __future__ import annotations

import datetime

import httpx
import pytest

from app.core.config import Settings
from app.core.security import decode_access_token
from tests.integration.conftest import (
    AUTH_URL,
    Builder,
    Execute,
    Rows,
    fresh,
    now_utc,
    sign_in,
)
from tests.unit.test_security import init_data

pytestmark = pytest.mark.integration


# ------------------------------------------------------------------- signing in


async def test_valid_init_data_issues_a_token_for_a_new_account(
    client: httpx.AsyncClient, settings: Settings, rows: Rows
) -> None:
    response = await client.post(
        AUTH_URL, json={"init_data": fresh(telegram_id=5150, language_code="zh-hans")}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == settings.jwt_access_token_ttl_seconds

    user_id = decode_access_token(body["access_token"], settings=settings)
    assert rows("SELECT id, telegram_id, locale FROM users") == [(user_id, 5150, "zh")]


async def test_a_new_account_gets_a_profile_and_an_allowance(
    client: httpx.AsyncClient, settings: Settings, rows: Rows
) -> None:
    """quota.py treats a missing entitlements row as a provisioning failure."""
    user_id = await sign_in(client, settings)

    assert rows("SELECT user_id, timezone, data_saver FROM user_profiles") == [
        (user_id, "Asia/Phnom_Penh", False)
    ]

    allowance = rows(
        "SELECT user_id, reset_at, daily_attempts_used, realtime_seconds_remaining "
        "FROM entitlements"
    )
    assert len(allowance) == 1
    assert allowance[0][0] == user_id
    # reset_at has no database default; it is computed from the learner's zone.
    assert allowance[0][1] > now_utc()
    assert allowance[0][2:] == (0, 0)


async def test_signing_in_again_reuses_the_account(
    client: httpx.AsyncClient, settings: Settings, rows: Rows
) -> None:
    first = await sign_in(client, settings)
    second = await sign_in(client, settings)

    assert first == second
    for table in ("users", "user_profiles", "entitlements"):
        assert rows(f"SELECT count(*) FROM {table}") == [(1,)], f"{table} gained a row"


async def test_signing_in_again_does_not_reset_a_spent_allowance(
    client: httpx.AsyncClient, settings: Settings, rows: Rows, execute: Execute
) -> None:
    """Re-authenticating must not be a way to buy back the day's attempts."""
    user_id = await sign_in(client, settings)
    execute("UPDATE entitlements SET daily_attempts_used = 7 WHERE user_id = :u", u=user_id)

    await sign_in(client, settings)

    assert rows("SELECT daily_attempts_used FROM entitlements") == [(7,)]


async def test_two_learners_get_two_accounts(
    client: httpx.AsyncClient, settings: Settings, rows: Rows
) -> None:
    one = await sign_in(client, settings, telegram_id=1)
    two = await sign_in(client, settings, telegram_id=2)

    assert one != two
    assert rows("SELECT count(*) FROM entitlements") == [(2,)]


async def test_sign_in_records_activity(
    client: httpx.AsyncClient, settings: Settings, rows: Rows
) -> None:
    before = now_utc()

    await sign_in(client, settings)

    assert rows("SELECT last_active_at FROM users")[0][0] >= before


# --------------------------------------------------------------------- refusals


@pytest.mark.parametrize(
    ("label", "build"),
    [
        ("forged_hash", lambda: fresh(bot_token="999:not-our-bot")),
        ("stale", lambda: init_data(auth_date=now_utc() - datetime.timedelta(days=2))),
        ("unsigned", lambda: "user=%7B%22id%22%3A5%7D&auth_date=1"),
        ("not_a_query_string", lambda: "nonsense"),
    ],
)
async def test_unverifiable_init_data_is_refused(
    client: httpx.AsyncClient, rows: Rows, label: str, build: Builder
) -> None:
    response = await client.post(AUTH_URL, json={"init_data": build()})

    assert response.status_code == 401, label
    assert response.json() == {"code": "auth.invalid", "message": "auth.invalid"}
    assert response.headers["www-authenticate"] == "Bearer"
    assert rows("SELECT count(*) FROM users") == [(0,)], f"{label} created an account"


async def test_the_refusal_does_not_say_which_check_failed(client: httpx.AsyncClient) -> None:
    """A forger learns nothing from the body; the reason is in the log."""
    forged = await client.post(AUTH_URL, json={"init_data": fresh(bot_token="999:nope")})
    stale = await client.post(
        AUTH_URL, json={"init_data": init_data(auth_date=now_utc() - datetime.timedelta(days=2))}
    )

    assert forged.json() == stale.json()


async def test_a_soft_deleted_account_cannot_sign_back_in(
    client: httpx.AsyncClient, settings: Settings, rows: Rows, execute: Execute
) -> None:
    """deleted_at is a deletion, not a dormant flag: re-auth must not undo it."""
    user_id = await sign_in(client, settings)
    execute("UPDATE users SET deleted_at = now(), last_active_at = NULL WHERE id = :u", u=user_id)

    response = await client.post(AUTH_URL, json={"init_data": fresh()})

    assert response.status_code == 401
    deleted_at, last_active_at = rows("SELECT deleted_at, last_active_at FROM users")[0]
    assert deleted_at is not None, "the account was resurrected"
    assert last_active_at is None, "a refused sign-in still counted as activity"


@pytest.mark.parametrize("body", [{}, {"init_data": ""}, {"initData": "x"}])
async def test_a_malformed_request_body_is_a_validation_error(
    client: httpx.AsyncClient, body: dict[str, str]
) -> None:
    response = await client.post(AUTH_URL, json=body)

    assert response.status_code == 422
