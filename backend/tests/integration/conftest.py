"""Scratch-database fixtures shared by the integration suite.

Extracted from the schema tests when the cost-ledger tests became a second
consumer. Every integration test that touches a database gets its own, created
and dropped around the test, so nothing here can corrupt the database a
developer is working in.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import URL, make_url

from app.core.config import Settings

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
