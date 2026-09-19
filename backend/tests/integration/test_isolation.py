"""The guarantee the shared database put at risk, asserted on purpose.

Tests used to get a database each. Now they share one and it is truncated on
the way in, which is the same promise -- a test never sees another test's rows
-- kept a different way. A different way is worth a test: the old arrangement
could not leak, and this one can, if a table is added to DATA_MODEL.sql and
something stops truncating it.

These two run in order and on the same worker (``--dist loadfile``), so the
second really does follow the first. The rows the first one writes are the
kind the suite writes everywhere -- a learner and their allowance -- so a leak
here is a leak everywhere.
"""

from __future__ import annotations

import pytest

from tests.integration.conftest import (
    MAX_PARALLEL_REDIS_DATABASES,
    Execute,
    Rows,
    worker_index,
    worker_redis_url,
)

pytestmark = pytest.mark.integration

TABLES = ("users", "user_profiles", "entitlements", "payments", "cost_ledger", "attempts")


def test_one_leaves_rows_behind(execute: Execute, rows: Rows) -> None:
    execute("INSERT INTO users (telegram_id, locale) VALUES (77001, 'km')")
    execute(
        "INSERT INTO cost_ledger (provider, unit, quantity, cost_usd_cents_est, ref) "
        "VALUES ('fake', 'calls', 1, 99, 'attempt')"
    )

    assert rows("SELECT count(*) FROM users") == [(1,)]
    assert rows("SELECT count(*) FROM cost_ledger") == [(1,)]


@pytest.mark.parametrize("table", TABLES)
def test_two_finds_none_of_them(table: str, rows: Rows) -> None:
    assert rows(f"SELECT count(*) FROM {table}") == [(0,)]


def present_tables(rows: Rows) -> set[str]:
    return {
        name
        for (name,) in rows(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
        )
    }


def test_the_reset_covers_every_table_the_schema_has(shared_tables: list[str], rows: Rows) -> None:
    """The structural half, and the one that catches the quiet failure.

    Checking that tables *are* empty only catches a table something wrote to,
    so a table nothing in the suite touches today could drop out of the reset
    and nobody would know until the day something does touch it. And a table
    left out is often invisible anyway: TRUNCATE CASCADE reaches anything with
    a foreign key into what it truncates, so the omission hides behind a
    relationship until somebody removes it.

    So this compares two lists from two sources: what the reset resets, and
    what the database actually has. Asking the reset what it resets and then
    checking those would be a question with only one answer.
    """
    assert shared_tables, "no tables found; the DDL did not run"
    assert set(shared_tables) == present_tables(rows)


def test_every_table_arrives_empty(rows: Rows) -> None:
    """And the behavioural half: the reset did run, for this test, just now."""
    for table in sorted(present_tables(rows)):
        assert rows(f'SELECT count(*) FROM "{table}"') == [(0,)], f"{table} kept its rows"


def test_each_worker_gets_its_own_redis_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scratch databases sort themselves out; Redis does not.

    Each xdist worker is its own process, so each builds its own PostgreSQL
    database without being asked. Redis is one server with numbered databases,
    and the bot fixtures clear keys by prefix -- two workers on the same number
    means one test wiping another's session between two of its own lines, once
    in a while, on a machine nobody can reproduce.
    """
    seen = set()
    for index in range(MAX_PARALLEL_REDIS_DATABASES):
        monkeypatch.setenv("PYTEST_XDIST_WORKER", f"gw{index}")
        monkeypatch.setenv("PYTEST_XDIST_WORKER_COUNT", str(MAX_PARALLEL_REDIS_DATABASES))
        assert worker_index() == index
        seen.add(worker_redis_url())

    assert len(seen) == MAX_PARALLEL_REDIS_DATABASES


def test_more_workers_than_databases_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """Loudly, rather than by quietly wrapping round to zero."""
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    monkeypatch.setenv("PYTEST_XDIST_WORKER_COUNT", str(MAX_PARALLEL_REDIS_DATABASES + 1))

    with pytest.raises(RuntimeError, match="Redis databases are reserved"):
        worker_index()


def test_a_single_process_run_needs_no_worker_number(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)

    assert worker_index() == 0
