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
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.engine import URL

from app.core.config import Settings
from app.core.security import decode_access_token
from app.main import create_app
from tests.unit.test_security import BOT_TOKEN, init_data

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]

AUTH_URL = "/api/v1/auth/telegram"


@pytest.fixture
def settings(migrated_db: URL) -> Settings:
    """The shipped template, pointed at a scratch database and given secrets.

    The template ships TELEGRAM_BOT_TOKEN and JWT_SECRET blank (nothing in CI
    talks to Telegram), and this endpoint refuses to authenticate anyone
    without both — so the test supplies them, as a deployment would.
    """
    overrides: dict[str, Any] = {
        "DATABASE_URL": migrated_db.render_as_string(hide_password=False),
        "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
        "JWT_SECRET": "integration-test-signing-key-0123",
    }
    return Settings(_env_file=REPO_ROOT / ".env.example", **overrides)


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    """A client wired to the real application, lifespan and all.

    The lifespan is entered explicitly rather than left out: it is where the
    provider self-check runs and where the connection pool is built, so a test
    that skipped it would be exercising an application nobody ever boots.
    """
    app: FastAPI = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://kruai.test") as http:
            yield http


@pytest.fixture
def db(settings: Settings) -> Iterator[sa.Engine]:
    engine = sa.create_engine(settings.database_url)
    try:
        yield engine
    finally:
        engine.dispose()


Rows = Callable[..., list[tuple[Any, ...]]]


@pytest.fixture
def rows(db: sa.Engine) -> Rows:
    def query(sql: str, **params: Any) -> list[tuple[Any, ...]]:
        with db.connect() as conn:
            return [tuple(row) for row in conn.execute(sa.text(sql), params)]

    return query


@pytest.fixture
def execute(db: sa.Engine) -> Callable[..., None]:
    def run(sql: str, **params: Any) -> None:
        with db.begin() as conn:
            conn.execute(sa.text(sql), params)

    return run


def now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def fresh(**kwargs: Any) -> str:
    """Signed initData dated now, so the freshness window is never the reason."""
    return init_data(auth_date=now_utc(), **kwargs)


async def sign_in(client: httpx.AsyncClient, settings: Settings, **kwargs: Any) -> uuid.UUID:
    response = await client.post(AUTH_URL, json={"init_data": fresh(**kwargs)})
    assert response.status_code == 200, response.text
    return decode_access_token(response.json()["access_token"], settings=settings)


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
    client: httpx.AsyncClient, settings: Settings, rows: Rows, execute: Callable[..., None]
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
    client: httpx.AsyncClient, rows: Rows, label: str, build: Callable[[], str]
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
    client: httpx.AsyncClient, settings: Settings, rows: Rows, execute: Callable[..., None]
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
