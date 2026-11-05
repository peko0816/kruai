"""The millicents migration carries existing spending across (D-136).

The schema guard compares shapes, not rows. This one checks the data: a ledger
row written in whole cents before the column existed has to arrive with the
same amount in thousandths, or every guard total and report would quietly drop
the history it was written before.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.engine import URL

from tests.integration.test_schema_matches_ddl import alembic_config

pytestmark = pytest.mark.integration

BEFORE = "79333c6c4815"


def test_rows_written_in_cents_arrive_in_thousandths(scratch_db: URL) -> None:
    config = alembic_config(scratch_db)
    command.upgrade(config, BEFORE)
    engine = sa.create_engine(scratch_db)
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO cost_ledger (provider, unit, quantity, cost_usd_cents_est, ref) "
                    "VALUES ('azure', 'calls', 1, 7, 'attempt')"
                )
            )

        command.upgrade(config, "head")

        with engine.connect() as connection:
            row = connection.execute(
                sa.text("SELECT cost_usd_cents_est, cost_usd_millicents_est FROM cost_ledger")
            ).one()
        assert tuple(row) == (7, 7000)

        command.downgrade(config, BEFORE)

        with engine.connect() as connection:
            kept = connection.execute(sa.text("SELECT cost_usd_cents_est FROM cost_ledger")).one()
        assert tuple(kept) == (7,), "downgrading loses nothing: the cents were never touched"
    finally:
        engine.dispose()
