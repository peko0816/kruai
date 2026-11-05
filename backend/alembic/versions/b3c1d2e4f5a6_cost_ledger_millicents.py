"""cost_ledger.cost_usd_millicents_est: cost below a cent, summed before rounding

Revision ID: b3c1d2e4f5a6
Revises: 79333c6c4815
Create Date: 2026-11-05

D-136. Existing rows were recorded in whole cents, so their thousandths are
exactly those cents times a thousand. Downgrading drops the column; the cents
column was never touched, so nothing is lost.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3c1d2e4f5a6"
down_revision: str | None = "79333c6c4815"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "cost_ledger",
        sa.Column(
            "cost_usd_millicents_est",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.execute("UPDATE cost_ledger SET cost_usd_millicents_est = cost_usd_cents_est * 1000")


def downgrade() -> None:
    op.drop_column("cost_ledger", "cost_usd_millicents_est")
