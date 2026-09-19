"""Scratch-database fixtures shared by the integration suite.

Extracted from the schema tests when the cost-ledger tests became a second
consumer. Every integration test that touches a database gets its own, created
and dropped around the test, so nothing here can corrupt the database a
developer is working in.

The application fixtures below (``settings``, ``client``, ``rows``, ``execute``)
arrived with the API tests in stage D. They boot the real app against a scratch
database — lifespan included, so the provider self-check runs exactly as it does
in production — and read rows back over a separate synchronous connection, so an
assertion sees what was committed rather than what an open transaction holds.
"""

from __future__ import annotations

import datetime
import os
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from bot.session import KEY_PREFIX
from fastapi import FastAPI
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.engine import URL, make_url

from app.core.config import Settings
from app.core.security import decode_access_token
from app.main import create_app
from tests.unit.test_security import BOT_TOKEN, init_data

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_ROOT.parent


def base_url() -> URL:
    """Connection details from the shipped template, not a developer's .env."""
    settings = Settings(_env_file=REPO_ROOT / ".env.example")
    return make_url(settings.database_url)


@pytest.fixture(scope="session")
def admin_engine() -> Iterator[sa.Engine]:
    """Connection to the maintenance database, for CREATE/DROP DATABASE.

    Skipping locally is a convenience for anyone without Docker running. In CI
    it is a trap: these are the only guards that the schema still matches
    DATA_MODEL.sql, and a green build that silently skipped them is worse than a
    red one. So under CI an unreachable database fails instead.
    """
    url = base_url().set(database="postgres")
    engine = sa.create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect():
            pass
    except sa.exc.OperationalError as exc:
        if os.environ.get("CI"):
            raise RuntimeError(
                "PostgreSQL unreachable in CI; the integration guards would have "
                "been skipped. Check the postgres service definition in the workflow."
            ) from exc
        pytest.skip(
            f"PostgreSQL unreachable ({exc.__class__.__name__}); run `docker compose up -d`"
        )
    yield engine
    engine.dispose()


def create_scratch_database(admin_engine: sa.Engine) -> str:
    name = f"kruai_test_{uuid.uuid4().hex[:12]}"
    with admin_engine.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    return name


def drop_scratch_database(admin_engine: sa.Engine, name: str) -> None:
    """Terminate stragglers first: an open pool would block the DROP."""
    with admin_engine.connect() as conn:
        conn.execute(
            sa.text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :n AND pid <> pg_backend_pid()"
            ),
            {"n": name},
        )
        conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))


@pytest.fixture
def scratch_db(admin_engine: sa.Engine) -> Iterator[URL]:
    """A throwaway database, dropped even if the test fails."""
    name = create_scratch_database(admin_engine)
    try:
        yield base_url().set(database=name)
    finally:
        drop_scratch_database(admin_engine, name)


@pytest.fixture
def migrated_db(scratch_db: URL) -> URL:
    """A scratch database with the schema applied, ready for real rows."""
    engine = sa.create_engine(scratch_db)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                (REPO_ROOT / "docs" / "DATA_MODEL.sql").read_text(encoding="utf-8")
            )
    finally:
        engine.dispose()
    return scratch_db


# --------------------------------------------------------------- the application


AUTH_URL = "/api/v1/auth/telegram"

#: 32 characters, the floor core/security.py enforces (RFC 7518 section 3.2).
TEST_JWT_SECRET = "integration-test-signing-key-0123"


@pytest.fixture
def settings(migrated_db: URL) -> Settings:
    """The shipped template, pointed at a scratch database and given secrets.

    The template ships TELEGRAM_BOT_TOKEN and JWT_SECRET blank (nothing in CI
    talks to Telegram), and authentication refuses to work without both — so
    the test supplies them, as a deployment would.
    """
    overrides: dict[str, Any] = {
        "DATABASE_URL": migrated_db.render_as_string(hide_password=False),
        "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
        "JWT_SECRET": TEST_JWT_SECRET,
    }
    return Settings(_env_file=REPO_ROOT / ".env.example", **overrides)


@asynccontextmanager
async def client_for(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    """A client wired to the real application, lifespan and all.

    The lifespan is entered explicitly rather than left out: it is where the
    provider self-check runs and where the connection pool is built, so a test
    that skipped it would be exercising an application nobody ever boots.

    Exposed as a context manager as well as the fixture below, so a test that
    needs different configuration — video switched on, a different minimum plan
    — can boot a second application against the same scratch database.
    """
    app: FastAPI = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://kruai.test") as http:
            yield http


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    async with client_for(settings) as http:
        yield http


@pytest.fixture
def db(settings: Settings) -> Iterator[sa.Engine]:
    engine = sa.create_engine(settings.database_url)
    try:
        yield engine
    finally:
        engine.dispose()


Rows = Callable[..., list[tuple[Any, ...]]]
Execute = Callable[..., None]
#: A zero-argument factory for one test case's request body, so a parametrised
#: case can be built at call time rather than at collection time.
Builder = Callable[[], str]


@pytest.fixture
def rows(db: sa.Engine) -> Rows:
    def query(sql: str, **params: Any) -> list[tuple[Any, ...]]:
        with db.connect() as conn:
            return [tuple(row) for row in conn.execute(sa.text(sql), params)]

    return query


@pytest.fixture
def execute(db: sa.Engine) -> Execute:
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
    """Authenticate through the real endpoint and return the learner's id.

    Through the endpoint rather than by minting a token directly, because
    signing in is also what provisions the profile and allowance rows (D-029)
    that everything downstream reads.
    """
    response = await client.post(AUTH_URL, json={"init_data": fresh(**kwargs)})
    assert response.status_code == 200, response.text
    return decode_access_token(response.json()["access_token"], settings=settings)


async def token_for(client: httpx.AsyncClient, **kwargs: Any) -> str:
    """The raw bearer token, for tests that send their own Authorization header."""
    response = await client.post(AUTH_URL, json={"init_data": fresh(**kwargs)})
    assert response.status_code == 200, response.text
    token: str = response.json()["access_token"]
    return token


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------- redis


@pytest.fixture
async def redis_client(settings: Settings) -> AsyncIterator[Redis]:
    """A Redis connection, with this test's bot keys cleared around it.

    Carrying constraint L-2: until D6 nothing in the repository connected to
    Redis, so CI had no service for it and a test like this would have skipped
    or failed depending on how it handled the refusal — and a silently skipped
    test is the failure mode that workflow goes out of its way to prevent. The
    service is in the workflow as of this change, so under CI an unreachable
    Redis fails here rather than quietly passing.

    Keys are namespaced and deleted rather than the database flushed: a
    developer's Redis may hold something else.
    """
    client: Redis = Redis.from_url(settings.redis_url)
    try:
        await client.ping()
    except RedisError as exc:
        if os.environ.get("CI"):
            raise RuntimeError(
                "Redis unreachable in CI; the bot session tests would have been "
                "skipped. Check the redis service definition in the workflow."
            ) from exc
        pytest.skip(f"Redis unreachable ({exc.__class__.__name__}); run `docker compose up -d`")

    await _clear_bot_keys(client)
    try:
        yield client
    finally:
        await _clear_bot_keys(client)
        await client.aclose()


async def _clear_bot_keys(client: Redis) -> None:
    keys = [key async for key in client.scan_iter(match=f"{KEY_PREFIX}*")]
    if keys:
        await client.delete(*keys)
