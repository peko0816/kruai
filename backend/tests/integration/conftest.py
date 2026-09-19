"""Scratch-database fixtures shared by the integration suite.

Extracted from the schema tests when the cost-ledger tests became a second
consumer. The database is still a scratch one, created and dropped around the
run and named with a uuid, so nothing here can touch the database a developer
is working in.

**One database per run, not one per test.** It was one per test until the suite
took nearly four minutes to answer a question, which is long enough that people
stop asking: CREATE DATABASE, the whole DDL and DROP DATABASE, three hundred
and fifty times, for tests whose slowest one does two seconds of real work.
What a test needs is not its own database but an empty one, and ``migrated_db``
gives it that by truncating every table on the way in. No test sees another's
rows. What is given up is changing the schema mid-suite, which only the schema
guards do, and they still get ``scratch_db``.

Connections are pooled for the same reason. Opening a new one to PostgreSQL
costs about forty-five milliseconds here and borrowing a pooled one costs five,
and the suite was opening three new ones per test to do nothing in particular.

The suite runs under xdist (``make test`` uses four workers). Each worker is a
separate process, so each builds its own scratch database without being asked;
Redis is not so obliging -- one server, numbered databases, and the bot tests
clear keys by prefix -- so ``worker_index`` hands each worker its own, and
refuses to start rather than let two share.

The application fixtures below (``settings``, ``client``, ``rows``, ``execute``)
arrived with the API tests in stage D. They boot the real app against the
scratch database — lifespan included, so the provider self-check runs exactly as
it does in production — and read rows back over a separate synchronous
connection, so an assertion sees what was committed rather than what an open
transaction holds.
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


#: What one test process may hold open. PostgreSQL ships with a hundred
#: connections and four workers share them, so the shipped pool of thirty would
#: have the suite failing with "too many clients" rather than with anything
#: anyone wrote. Small is also what the concurrency tests want: they fire one
#: request more than the pool can serve, and proving that is cheaper against a
#: pool of seven than a pool of thirty.
TEST_DB_POOL_SIZE = 5
TEST_DB_MAX_OVERFLOW = 2

#: Redis ships sixteen logical databases and the suite may not have all of
#: them; more workers than this and two of them would share one, which means
#: clearing bot keys in one test wipes another worker's session mid-assertion.
MAX_PARALLEL_REDIS_DATABASES = 8


def worker_index() -> int:
    """Which xdist worker this is, or 0 when running single-process.

    Everything a worker must not share is keyed off this. Scratch databases
    take care of themselves -- their names carry a uuid and each worker is its
    own process, so each builds its own -- but Redis is one server with a fixed
    set of numbered databases, and the bot tests clear keys by prefix.
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER", "")
    if not worker.startswith("gw"):
        return 0

    count = int(os.environ.get("PYTEST_XDIST_WORKER_COUNT", "1"))
    if count > MAX_PARALLEL_REDIS_DATABASES:
        # Wrapping round would be the quiet version of this, and the symptom
        # would be a bot test that fails once a fortnight because another
        # worker cleared its session between two lines.
        raise RuntimeError(
            f"-n {count} but only {MAX_PARALLEL_REDIS_DATABASES} Redis databases are "
            f"reserved for tests; run with -n {MAX_PARALLEL_REDIS_DATABASES} or fewer, "
            "or raise MAX_PARALLEL_REDIS_DATABASES and check the server allows it"
        )
    return int(worker.removeprefix("gw"))


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
    """A throwaway database of its own, dropped even if the test fails.

    For the tests that need an empty one: the schema guards build the schema
    themselves, twice over and in both directions, and cannot share.
    """
    name = create_scratch_database(admin_engine)
    try:
        yield base_url().set(database=name)
    finally:
        drop_scratch_database(admin_engine, name)


def apply_ddl(url: URL) -> None:
    engine = sa.create_engine(url)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                (REPO_ROOT / "docs" / "DATA_MODEL.sql").read_text(encoding="utf-8")
            )
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def shared_db(admin_engine: sa.Engine) -> Iterator[URL]:
    """One database with the schema applied, built once for the whole run.

    It used to be one per test. Correct, and the reason the suite took nearly
    four minutes to answer a question: CREATE DATABASE, the full DDL and DROP
    DATABASE, three hundred and fifty times over, for tests where the slowest
    one does two seconds of actual work.

    What a test actually needs is not its own database but an empty one, and
    ``migrated_db`` gives it that by truncating. The isolation is the same --
    no test sees another's rows -- and what is given up is the ability to
    change the schema mid-suite, which only the schema guards do, and they
    have ``scratch_db``.
    """
    name = create_scratch_database(admin_engine)
    url = base_url().set(database=name)
    try:
        apply_ddl(url)
        yield url
    finally:
        drop_scratch_database(admin_engine, name)


@pytest.fixture(scope="session")
def shared_engine(shared_db: URL) -> Iterator[sa.Engine]:
    """One pooled connection to the shared database, for the whole run.

    Opening a *new* connection to PostgreSQL costs about 45 milliseconds here;
    borrowing a pooled one costs five. Three new ones per test is most of what
    the suite used to spend, and none of it was testing anything.
    """
    engine = sa.create_engine(shared_db, pool_size=2, max_overflow=3)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def shared_tables(shared_db: URL) -> list[str]:
    """Every table the DDL created, read back rather than listed by hand.

    A table added to DATA_MODEL.sql and forgotten here would be one that keeps
    its rows between tests -- the kind of leak that shows up much later as a
    test that passes alone and fails in the suite.
    """
    engine = sa.create_engine(shared_db)
    try:
        with engine.connect() as conn:
            found = conn.execute(
                sa.text(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
                )
            )
            return [row[0] for row in found]
    finally:
        engine.dispose()


@pytest.fixture
def migrated_db(shared_db: URL, shared_engine: sa.Engine, shared_tables: list[str]) -> URL:
    """The shared database, emptied. As good as new, and far quicker to get.

    Emptied on the way in rather than on the way out, so a test that fails
    half-written cannot leave anything behind for the next one -- and so a
    failing test's rows are still there to look at afterwards.
    """
    names = ", ".join(f'"{table}"' for table in shared_tables)
    with shared_engine.begin() as conn:
        # RESTART IDENTITY for completeness rather than for any assertion: the
        # schema has one sequence (cost_ledger.id) and nothing reads it back,
        # but a reset that leaves a counter climbing all run is not the "as
        # good as new" this fixture promises, and a debugging session where ids
        # start at one is a reproducible one.
        conn.exec_driver_sql(f"TRUNCATE {names} RESTART IDENTITY CASCADE")
    return shared_db


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
        "DB_POOL_SIZE": TEST_DB_POOL_SIZE,
        "DB_MAX_OVERFLOW": TEST_DB_MAX_OVERFLOW,
        "REDIS_URL": worker_redis_url(),
        "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
        "JWT_SECRET": TEST_JWT_SECRET,
    }
    return Settings(_env_file=REPO_ROOT / ".env.example", **overrides)


def worker_redis_url() -> str:
    """The template's Redis, on this worker's own logical database."""
    template = Settings(_env_file=REPO_ROOT / ".env.example").redis_url
    head, _, _ = template.rpartition("/")
    return f"{head}/{worker_index()}"


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
def db(settings: Settings, shared_engine: sa.Engine) -> sa.Engine:
    """The read-back connection, over the shared pool.

    ``settings`` is depended on rather than used, so that asking for ``rows``
    is still what empties the database and boots the configuration -- the
    ordering the whole suite is written against.
    """
    assert settings.database_url
    return shared_engine


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
