"""docs/DATA_MODEL.sql is authoritative; this proves the code still agrees.

Three things have to stay in step — the hand-written DDL, the SQLAlchemy models,
and the alembic migrations — and nothing about a mismatch is visible at import
time. A column that drifted to the wrong default or a CHECK constraint that
silently disappeared surfaces as corrupt data months later, so each edge of that
triangle is asserted here:

  models    == DDL   via alembic's own metadata comparison
  migration == DDL   via a catalog fingerprint of two freshly built databases
  downgrade           by round-tripping and re-fingerprinting

The fingerprint is built from pg_catalog rather than pg_dump so the suite needs
only a database connection, not postgres client binaries on the host.
"""

from __future__ import annotations

import os
import re
import uuid

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy.engine import URL

from app.models import Base
from tests.integration.conftest import BACKEND_ROOT, REPO_ROOT, base_url

pytestmark = pytest.mark.integration

_DATA_MODEL_SQL = REPO_ROOT / "docs" / "DATA_MODEL.sql"

# Alembic bookkeeping is not part of the data model.
_IGNORED_TABLES = ("alembic_version",)


def build_from_ddl(url: URL) -> None:
    """Apply docs/DATA_MODEL.sql verbatim."""
    engine = sa.create_engine(url)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(_DATA_MODEL_SQL.read_text(encoding="utf-8"))
    finally:
        engine.dispose()


def alembic_config(url: URL) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    # env.py reads this override so the real DATABASE_URL is never touched.
    os.environ["ALEMBIC_DATABASE_URL"] = url.render_as_string(hide_password=False)
    return config


def build_from_migration(url: URL) -> Config:
    config = alembic_config(url)
    command.upgrade(config, "head")
    return config


def fingerprint(url: URL) -> dict[str, list[tuple[str, ...]]]:
    """Everything that defines the schema, as comparable tuples.

    Columns, constraints and indexes are read from pg_catalog with
    ``pg_get_constraintdef`` / ``pg_get_indexdef``, which render the normalised
    form PostgreSQL actually stored — so an expression written differently in
    two places still compares equal if it means the same thing.
    """
    placeholders = ", ".join(f"'{t}'" for t in _IGNORED_TABLES)
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            columns = conn.execute(
                sa.text(f"""
                SELECT table_name, column_name, ordinal_position::text, data_type,
                       is_nullable, COALESCE(column_default, ''),
                       COALESCE(character_maximum_length::text, ''),
                       COALESCE(numeric_precision::text, '')
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name NOT IN ({placeholders})
                ORDER BY table_name, ordinal_position
            """)
            ).all()

            constraints = conn.execute(
                sa.text(f"""
                SELECT rel.relname, con.conname, pg_get_constraintdef(con.oid)
                FROM pg_constraint con
                JOIN pg_class rel ON rel.oid = con.conrelid
                JOIN pg_namespace ns ON ns.oid = rel.relnamespace
                WHERE ns.nspname = 'public' AND rel.relname NOT IN ({placeholders})
                ORDER BY rel.relname, con.conname
            """)
            ).all()

            indexes = conn.execute(
                sa.text(f"""
                SELECT tablename, indexname, indexdef
                FROM pg_indexes
                WHERE schemaname = 'public' AND tablename NOT IN ({placeholders})
                ORDER BY tablename, indexname
            """)
            ).all()
    finally:
        engine.dispose()

    return {
        "columns": [tuple(str(v) for v in row) for row in columns],
        "constraints": [tuple(str(v) for v in row) for row in constraints],
        "indexes": [tuple(str(v) for v in row) for row in indexes],
    }


def table_names(url: URL) -> set[str]:
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
                )
            ).scalars()
            return {name for name in rows if name not in _IGNORED_TABLES}
    finally:
        engine.dispose()


# ------------------------------------------------------------ models vs DDL


def test_models_match_data_model_sql(scratch_db: URL) -> None:
    """Every column, type and server default in the models exists in the DDL."""
    build_from_ddl(scratch_db)

    engine = sa.create_engine(scratch_db)
    try:
        with engine.connect() as conn:
            context = MigrationContext.configure(
                conn, opts={"compare_type": True, "compare_server_default": True}
            )
            differences = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()

    assert differences == [], "SQLAlchemy models drifted from docs/DATA_MODEL.sql:\n" + "\n".join(
        f"  {d}" for d in differences
    )


def test_models_emit_the_same_schema_as_the_ddl(scratch_db: URL, admin_engine: sa.Engine) -> None:
    """Fingerprint a database built straight from the models against the DDL.

    compare_metadata above is necessary but not sufficient: it ignores CHECK
    constraints entirely, so deleting one from a model passes it. Creating the
    tables from metadata and comparing pg_get_constraintdef output closes that
    hole — and CHECK constraints are load-bearing here, since they encode the
    item_type, plan and renewal_mode enums.
    """
    build_from_ddl(scratch_db)
    expected = fingerprint(scratch_db)

    other = f"kruai_test_{uuid.uuid4().hex[:12]}"
    with admin_engine.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{other}"'))
    from_models = base_url().set(database=other)
    try:
        engine = sa.create_engine(from_models)
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')
            Base.metadata.create_all(engine)
        finally:
            engine.dispose()
        actual = fingerprint(from_models)
    finally:
        with admin_engine.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{other}" WITH (FORCE)'))

    for part in ("columns", "constraints", "indexes"):
        assert actual[part] == expected[part], (
            f"{part} differ between the models and docs/DATA_MODEL.sql\n"
            f"  only in DDL   : {sorted(set(expected[part]) - set(actual[part]))}\n"
            f"  only in models: {sorted(set(actual[part]) - set(expected[part]))}"
        )


# --------------------------------------------------------- migration vs DDL


def test_migration_reproduces_data_model_sql(scratch_db: URL, admin_engine: sa.Engine) -> None:
    """A database built by alembic is indistinguishable from one built by the DDL."""
    build_from_ddl(scratch_db)
    expected = fingerprint(scratch_db)

    other = f"kruai_test_{uuid.uuid4().hex[:12]}"
    with admin_engine.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{other}"'))
    migrated = base_url().set(database=other)
    try:
        build_from_migration(migrated)
        actual = fingerprint(migrated)
    finally:
        with admin_engine.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{other}" WITH (FORCE)'))

    for part in ("columns", "constraints", "indexes"):
        assert actual[part] == expected[part], (
            f"{part} differ between the migration and docs/DATA_MODEL.sql\n"
            f"  only in DDL      : {sorted(set(expected[part]) - set(actual[part]))}\n"
            f"  only in migration: {sorted(set(actual[part]) - set(expected[part]))}"
        )


def test_migration_creates_every_documented_table(scratch_db: URL) -> None:
    build_from_migration(scratch_db)

    documented = set(
        re.findall(r"^CREATE TABLE (\w+) \(", _DATA_MODEL_SQL.read_text(encoding="utf-8"), re.M)
    )
    assert documented, "DDL parse produced no tables; the regex is wrong"
    assert table_names(scratch_db) == documented


# ------------------------------------------------------------------ rollback


def test_downgrade_removes_everything_and_upgrade_restores_it(scratch_db: URL) -> None:
    """CODING_STANDARDS section 8: downgrade must genuinely roll back."""
    config = build_from_migration(scratch_db)
    after_upgrade = fingerprint(scratch_db)
    assert table_names(scratch_db), "upgrade created no tables"

    command.downgrade(config, "base")
    assert table_names(scratch_db) == set(), "downgrade left tables behind"

    command.upgrade(config, "head")
    assert fingerprint(scratch_db) == after_upgrade, "round-trip changed the schema"


def test_pgcrypto_is_created_by_the_migration(scratch_db: URL) -> None:
    """UUID defaults call gen_random_uuid(); without the extension inserts fail."""
    build_from_migration(scratch_db)

    engine = sa.create_engine(scratch_db)
    try:
        with engine.connect() as conn:
            generated = conn.execute(sa.text("SELECT gen_random_uuid()")).scalar_one()
    finally:
        engine.dispose()

    assert isinstance(generated, uuid.UUID)
