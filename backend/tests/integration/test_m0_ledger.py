"""The M0 scripts' ledger, against a real PostgreSQL.

Lives here rather than in m0/tests because the scratch databases do. The unit
tests can show that an unreachable database stops a billed run; only a live
one can show the other half — reachable but never migrated, which is the
state a fresh checkout is in and the one most likely on the day of the first
real run. Delete with m0/ once E8 and E9 have landed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from m0.providers import LedgerUnavailableError, open_ledger
from m0.scoring_check import Sample, score_all
from sqlalchemy.engine import URL

from app.core.config import Settings
from tests.integration.conftest import REPO_ROOT

pytestmark = pytest.mark.integration


def settings_for(url: URL) -> Settings:
    return Settings(
        _env_file=REPO_ROOT / ".env.example",
        database_url=url.render_as_string(hide_password=False),
        jwt_secret="x" * 32,
    )


async def test_a_database_without_the_schema_stops_a_billed_run(scratch_db: URL) -> None:
    with pytest.raises(LedgerUnavailableError, match="make migrate"):
        async with open_ledger(settings_for(scratch_db), billed=True):
            pytest.fail("the body must not run")


async def test_a_billed_run_leaves_one_row_per_call(migrated_db: URL, tmp_path: Path) -> None:
    """FakeScorer stands in for Azure; what is under test is that the rows land."""
    (tmp_path / "a.wav").write_bytes(b"RIFF")
    samples = [
        Sample(path=tmp_path / "a.wav", reference_text="我要水。", group="good"),
        Sample(path=tmp_path / "a.wav", reference_text="我要水。__BAD__", group="bad"),
    ]
    settings = settings_for(migrated_db)

    async with open_ledger(settings, billed=True) as ledger:
        await score_all(samples, settings=settings, ledger=ledger)

    engine = sa.create_engine(migrated_db)
    try:
        with engine.connect() as connection:
            written = connection.execute(
                sa.text("SELECT provider, unit, cost_usd_cents_est, ref, user_id FROM cost_ledger")
            ).all()
    finally:
        engine.dispose()
    assert [tuple(row) for row in written] == [("fake", "calls", 1, "content_production", None)] * 2
