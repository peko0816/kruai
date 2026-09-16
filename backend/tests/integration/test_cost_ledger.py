"""What actually reaches cost_ledger, against a real database.

The two properties worth proving here cannot be checked without one:

  · rows survive a rollback of the surrounding business transaction, because
    the money left the account either way;
  · a cost recorded before a later failure is still written, because the call
    already happened.

The second is the one that would quietly not hold if the ledger ever joined the
caller's session — and it is exactly the case where the record matters most.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.core.db import create_engine, create_session_factory
from app.models.commerce import CostLedger as CostLedgerRow
from app.models.users import User
from app.services.cost_ledger import CostLedger, UnledgeredCallError
from app.services.scoring.base import AssessMode, Language
from app.services.scoring.fake import FakeScorer

pytestmark = pytest.mark.integration


def settings_for(url: URL, **overrides: str) -> Settings:
    values: dict[str, Any] = {
        "DATABASE_URL": url.render_as_string(hide_password=False),
        "REDIS_URL": "redis://localhost:6379/0",
        "TELEGRAM_BOT_TOKEN": "",
        "AZURE_SPEECH_KEY": "",
        "AZURE_SPEECH_REGION": "",
        "GOOGLE_APPLICATION_CREDENTIALS": "",
        "ELEVENLABS_API_KEY": "",
        "OPENAI_API_KEY": "",
        "PAYWAY_MERCHANT_ID": "",
        "PAYWAY_API_KEY": "",
        "PAYWAY_BASE_URL": "",
        "BAKONG_TOKEN": "",
        "JWT_SECRET": "",
        **overrides,
    }
    return Settings(_env_file=None, **values)


@pytest.fixture
async def engine(migrated_db: URL) -> AsyncEngine:
    return create_engine(settings_for(migrated_db))


async def rows(engine: AsyncEngine) -> list[CostLedgerRow]:
    factory = create_session_factory(engine)
    async with factory() as session:
        result = await session.execute(sa.select(CostLedgerRow).order_by(CostLedgerRow.id))
        return list(result.scalars())


def ledger_for(engine: AsyncEngine, url: URL, **overrides: str) -> CostLedger:
    return CostLedger(
        session_factory=create_session_factory(engine), settings=settings_for(url, **overrides)
    )


# ------------------------------------------------------------- the write path


async def test_a_recorded_call_lands_in_the_table(engine: AsyncEngine, migrated_db: URL) -> None:
    ledger = ledger_for(engine, migrated_db)

    async with ledger.external_call(provider="fake", ref="attempt") as entry:
        entry.record(unit="calls", quantity=1, cost_usd_cents=3)

    written = await rows(engine)
    assert len(written) == 1
    assert written[0].provider == "fake"
    assert written[0].unit == "calls"
    assert written[0].quantity == pytest.approx(1.0)
    assert written[0].cost_usd_cents_est == 3
    assert written[0].ref == "attempt"
    await engine.dispose()


async def test_several_costs_become_several_rows(engine: AsyncEngine, migrated_db: URL) -> None:
    ledger = ledger_for(engine, migrated_db)

    async with ledger.external_call(provider="openai", ref="attempt") as entry:
        entry.record(unit="tokens", quantity=1200, cost_usd_cents=2)
        entry.record(unit="tokens", quantity=300, cost_usd_cents=3)

    written = await rows(engine)
    assert [row.cost_usd_cents_est for row in written] == [2, 3]
    await engine.dispose()


async def test_a_pack_build_is_recorded_without_a_user(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    """PRD 11.3 keeps content production out of per-user operating cost."""
    ledger = ledger_for(engine, migrated_db)

    async with ledger.external_call(provider="fake", ref="content_production") as entry:
        entry.record(unit="calls", quantity=1, cost_usd_cents=0)

    written = await rows(engine)
    assert written[0].user_id is None
    assert written[0].ref == "content_production"
    await engine.dispose()


async def test_a_cost_can_be_attributed_to_a_user(engine: AsyncEngine, migrated_db: URL) -> None:
    factory = create_session_factory(engine)
    async with factory() as session:
        user = User(telegram_id=910001)
        session.add(user)
        await session.commit()
        user_id = user.id

    ledger = ledger_for(engine, migrated_db)
    async with ledger.external_call(provider="fake", ref="attempt", user_id=user_id) as entry:
        entry.record(unit="seconds", quantity=4.2, cost_usd_cents=1)

    written = await rows(engine)
    assert written[0].user_id == user_id
    assert written[0].quantity == pytest.approx(4.2)
    await engine.dispose()


# ---------------------------------------------- independence from the caller


async def test_a_rolled_back_business_transaction_keeps_the_cost_row(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    """The money left the account whether or not the attempt was saved."""
    ledger = ledger_for(engine, migrated_db)
    factory = create_session_factory(engine)

    async with factory() as business:
        business.add(User(telegram_id=910002))
        async with ledger.external_call(provider="fake", ref="attempt") as entry:
            entry.record(unit="calls", quantity=1, cost_usd_cents=7)
        await business.rollback()

    written = await rows(engine)
    assert len(written) == 1, "the ledger row vanished with the business transaction"
    assert written[0].cost_usd_cents_est == 7

    async with factory() as session:
        remaining = await session.execute(sa.select(sa.func.count()).select_from(User.__table__))
        assert remaining.scalar_one() == 0, "the business write should have rolled back"
    await engine.dispose()


async def test_a_cost_recorded_before_a_later_failure_is_still_written(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    """The call happened. An exception afterwards must not erase the evidence."""
    ledger = ledger_for(engine, migrated_db)

    with pytest.raises(RuntimeError, match="something later"):
        async with ledger.external_call(provider="fake", ref="attempt") as entry:
            entry.record(unit="calls", quantity=1, cost_usd_cents=5)
            raise RuntimeError("something later")

    written = await rows(engine)
    assert [row.cost_usd_cents_est for row in written] == [5]
    await engine.dispose()


async def test_an_unledgered_call_writes_nothing(engine: AsyncEngine, migrated_db: URL) -> None:
    ledger = ledger_for(engine, migrated_db)

    with pytest.raises(UnledgeredCallError):
        async with ledger.external_call(provider="fake", ref="attempt"):
            pass

    assert await rows(engine) == []
    await engine.dispose()


# ------------------------------------------------- wrapping a real provider


async def test_wrapping_a_scorer_records_the_cost_it_reports(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    """The shape ARCHITECTURE 2.1 prescribes for the attempts flow."""
    ledger = ledger_for(engine, migrated_db)
    scorer = FakeScorer()

    async with ledger.external_call(provider=scorer.name, ref="attempt") as entry:
        result = await scorer.assess(
            b"\x00" * 1200,
            language=Language.ZH_CN,
            mode=AssessMode.SCRIPTED,
            reference_text="我要水",
        )
        entry.record(unit="calls", quantity=1, cost_usd_cents=result.cost_usd_cents)

    written = await rows(engine)
    assert written[0].provider == "fake"
    assert written[0].cost_usd_cents_est == result.cost_usd_cents
    assert written[0].cost_usd_cents_est > 0, "a scored attempt should cost something"
    await engine.dispose()


async def test_a_refused_assessment_is_recorded_as_free(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    """Nothing was assessed, so the cost is zero — but the call is still logged,
    which is what stops "it failed" from becoming "it never happened"."""
    ledger = ledger_for(engine, migrated_db)
    scorer = FakeScorer()

    async with ledger.external_call(provider=scorer.name, ref="attempt") as entry:
        result = await scorer.assess(
            b"", language=Language.ZH_CN, mode=AssessMode.SCRIPTED, reference_text="我要水"
        )
        entry.record(unit="calls", quantity=1, cost_usd_cents=result.cost_usd_cents)

    assert result.ok is False
    written = await rows(engine)
    assert len(written) == 1
    assert written[0].cost_usd_cents_est == 0
    await engine.dispose()


async def test_ids_are_assigned_by_the_database(engine: AsyncEngine, migrated_db: URL) -> None:
    """BIGSERIAL: this table is append-only and grows fastest."""
    ledger = ledger_for(engine, migrated_db)

    for _ in range(3):
        async with ledger.external_call(provider="fake", ref="attempt") as entry:
            entry.record(unit="calls", quantity=1, cost_usd_cents=1)

    written = await rows(engine)
    ids = [row.id for row in written]
    assert ids == sorted(ids)
    assert len(set(ids)) == 3
    await engine.dispose()


async def test_a_deleted_user_leaves_the_cost_behind(engine: AsyncEngine, migrated_db: URL) -> None:
    """ON DELETE SET NULL: cost history outlives the account it came from."""
    factory = create_session_factory(engine)
    async with factory() as session:
        user = User(telegram_id=910003)
        session.add(user)
        await session.commit()
        user_id = user.id

    ledger = ledger_for(engine, migrated_db)
    async with ledger.external_call(provider="fake", ref="attempt", user_id=user_id) as entry:
        entry.record(unit="calls", quantity=1, cost_usd_cents=9)

    async with factory() as session:
        await session.execute(sa.delete(User).where(User.id == user_id))
        await session.commit()

    written = await rows(engine)
    assert len(written) == 1
    assert written[0].user_id is None
    assert written[0].cost_usd_cents_est == 9
    await engine.dispose()


async def test_the_guard_can_be_disabled_without_breaking_the_write_path(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    ledger = ledger_for(engine, migrated_db, COST_LEDGER_REQUIRED="false")

    async with ledger.external_call(provider="fake", ref="attempt"):
        pass
    async with ledger.external_call(provider="fake", ref="attempt") as entry:
        entry.record(unit="calls", quantity=1, cost_usd_cents=2)

    written = await rows(engine)
    assert [row.cost_usd_cents_est for row in written] == [2]
    await engine.dispose()


async def test_ledger_rows_are_not_tied_to_a_user_id_that_does_not_exist(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    """The FK is real; a bogus attribution must fail loudly rather than land."""
    ledger = ledger_for(engine, migrated_db)

    with pytest.raises(sa.exc.IntegrityError):
        async with ledger.external_call(
            provider="fake", ref="attempt", user_id=uuid.uuid4()
        ) as entry:
            entry.record(unit="calls", quantity=1, cost_usd_cents=1)
    await engine.dispose()
