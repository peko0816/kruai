"""Assignment against a real database: written once, then defended.

The guarantee worth testing is not that the row appears. It is that once a
learner is in the table, nothing recomputes them out of it — not a changed
split, not a redeployed hash, not ten simultaneous requests. PRD 7.2 decides a
$375-1,500 commitment by comparing completion rates between the arms, and a
learner who moved between arms mid-run contributes sessions to both.

So the tests here move the configuration underneath an existing assignment and
assert that nothing happens.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.core.db import create_engine, create_session_factory
from app.models.learning import Experiment
from app.models.users import User
from app.services.experiments import EXPLAIN_MEDIA, Experiments, UnknownExperimentError
from app.services.media import VARIANT_AUDIO, VARIANT_VIDEO

pytestmark = pytest.mark.integration

KEY = EXPLAIN_MEDIA.key


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
        "EXPERIMENT_EXPLAIN_MEDIA_ENABLED": "true",
        **overrides,
    }
    return Settings(_env_file=None, **values)


@pytest.fixture
async def engine(migrated_db: URL) -> AsyncIterator[AsyncEngine]:
    """Disposed on the way out.

    The scratch database is dropped after the test, and dropping it terminates
    every backend still connected. Pooled connections left behind are then dead
    sockets that psycopg complains about whenever the collector gets to them —
    which lands the complaint on whatever test happens to be running.
    """
    engine = create_engine(settings_for(migrated_db))
    yield engine
    await engine.dispose()


def service(engine: AsyncEngine, url: URL, **overrides: str) -> Experiments:
    """A service on the same engine but its own configuration.

    Separate settings per call is the point of several tests here: it is how a
    split change or a kill switch reaches a learner who is already assigned.
    """
    return Experiments(
        session_factory=create_session_factory(engine), settings=settings_for(url, **overrides)
    )


async def provision(engine: AsyncEngine) -> uuid.UUID:
    factory = create_session_factory(engine)
    async with factory() as session:
        user = User(telegram_id=int(uuid.uuid4().int % 1_000_000_000))
        session.add(user)
        await session.commit()
        return user.id


async def rows(engine: AsyncEngine, user_id: uuid.UUID) -> list[Experiment]:
    factory = create_session_factory(engine)
    async with factory() as session:
        result = await session.execute(
            sa.select(Experiment)
            .where(Experiment.user_id == user_id)
            .order_by(Experiment.experiment_key)
        )
        return list(result.scalars())


async def find_user_assigned_to(
    engine: AsyncEngine, url: URL, wanted: str, *, split: str
) -> uuid.UUID:
    """A provisioned learner whose bucket lands in the wanted arm at this split."""
    svc = service(engine, url, EXPERIMENT_EXPLAIN_MEDIA_SPLIT=split)
    for _ in range(50):
        user_id = await provision(engine)
        if await svc.assign(user_id, experiment_key=KEY) == wanted:
            return user_id
    raise AssertionError(f"no learner bucketed to {wanted!r} in 50 tries")


# ------------------------------------------------------------- writing it down


async def test_a_first_assignment_is_recorded(engine: AsyncEngine, migrated_db: URL) -> None:
    user_id = await provision(engine)

    variant = await service(engine, migrated_db).assign(user_id, experiment_key=KEY)

    stored = await rows(engine, user_id)
    assert len(stored) == 1
    assert stored[0].experiment_key == KEY
    assert stored[0].variant == variant
    assert variant in {VARIANT_AUDIO, VARIANT_VIDEO}


async def test_assigning_twice_writes_one_row(engine: AsyncEngine, migrated_db: URL) -> None:
    """Safe to call on every lesson request, which is how D2 will use it."""
    svc = service(engine, migrated_db)
    user_id = await provision(engine)

    first = await svc.assign(user_id, experiment_key=KEY)
    before = (await rows(engine, user_id))[0].assigned_at
    repeats = [await svc.assign(user_id, experiment_key=KEY) for _ in range(5)]

    stored = await rows(engine, user_id)
    assert len(stored) == 1
    assert repeats == [first] * 5
    assert stored[0].assigned_at == before


async def test_each_experiment_gets_its_own_row(engine: AsyncEngine, migrated_db: URL) -> None:
    """The primary key is (user_id, experiment_key), so one learner can be in
    several A/Bs at once."""
    user_id = await provision(engine)
    svc = service(engine, migrated_db)
    await svc.assign(user_id, experiment_key=KEY)

    # Registered experiments are the only ones assignable; a second row is what
    # the schema permits, which is what this asserts.
    factory = create_session_factory(engine)
    async with factory() as session:
        session.add(Experiment(user_id=user_id, experiment_key="other_test", variant="on"))
        await session.commit()

    assert [r.experiment_key for r in await rows(engine, user_id)] == [
        "explain_media",
        "other_test",
    ]


async def test_two_learners_are_assigned_independently(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    svc = service(engine, migrated_db)
    first, second = await provision(engine), await provision(engine)

    await svc.assign(first, experiment_key=KEY)
    await svc.assign(second, experiment_key=KEY)

    assert len(await rows(engine, first)) == 1
    assert len(await rows(engine, second)) == 1


# ------------------------------------------------------------- defending it


async def test_a_wider_split_does_not_move_an_assigned_learner(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    """The reason the table exists.

    Recomputing every time would migrate this learner from audio to video when
    the ratio moves, carrying their earlier sessions across, and the 15%
    comparison in PRD 7.2 would then be an average over people who were in both
    arms. Nothing would report a problem.
    """
    user_id = await find_user_assigned_to(engine, migrated_db, VARIANT_AUDIO, split="0.0")

    widened = service(engine, migrated_db, EXPERIMENT_EXPLAIN_MEDIA_SPLIT="1.0")
    after = await widened.assign(user_id, experiment_key=KEY)

    assert after == VARIANT_AUDIO
    assert (await rows(engine, user_id))[0].variant == VARIANT_AUDIO


async def test_a_narrower_split_does_not_move_one_either(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    user_id = await find_user_assigned_to(engine, migrated_db, VARIANT_VIDEO, split="1.0")

    narrowed = service(engine, migrated_db, EXPERIMENT_EXPLAIN_MEDIA_SPLIT="0.0")

    assert await narrowed.assign(user_id, experiment_key=KEY) == VARIANT_VIDEO


async def test_a_stored_variant_outranks_the_calculation(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    """A row put there by hand — a support override, a backfill — is honoured.

    The table is the record of what a learner was actually shown, so anything
    that disagrees with it is the thing that is wrong.
    """
    user_id = await provision(engine)
    factory = create_session_factory(engine)
    async with factory() as session:
        session.add(Experiment(user_id=user_id, experiment_key=KEY, variant=VARIANT_VIDEO))
        await session.commit()

    both_ways = [
        await service(engine, migrated_db, EXPERIMENT_EXPLAIN_MEDIA_SPLIT=s).assign(
            user_id, experiment_key=KEY
        )
        for s in ("0.0", "1.0")
    ]

    assert both_ways == [VARIANT_VIDEO, VARIANT_VIDEO]


async def test_ten_simultaneous_requests_agree_on_one_row(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    """Ten at once, as a learner opening a lesson list gives.

    This does not prove the conflict path works. Instrumenting it showed nine of
    the ten calls finding the row already committed — they serialise in practice
    rather than overlapping, so the INSERT that has to survive a collision is
    never reached. The next test forces that collision instead of hoping for it.
    """
    svc = service(engine, migrated_db)
    user_id = await provision(engine)

    answers = await asyncio.gather(*(svc.assign(user_id, experiment_key=KEY) for _ in range(10)))

    assert len(set(answers)) == 1
    assert None not in answers
    assert len(await rows(engine, user_id)) == 1


async def test_a_request_that_loses_the_race_reads_back_the_winner(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    """The collision, staged rather than hoped for.

    A rival holds an uncommitted row for this learner, which is exactly the
    state a competing request occupies for the microseconds that matter. The
    racer cannot see it, so it tries to insert, and PostgreSQL makes it wait on
    the rival's speculative index entry. What matters is what happens when the
    wait ends: ON CONFLICT DO NOTHING turns the collision into zero rows and the
    racer reads back the winner's variant. A plain INSERT would raise instead,
    and the learner's lesson request would fail on a row that already says what
    they should be shown.

    A lost race here is harmless in a way the same race in entitlements is not
    — every racer computes the same variant — so DO NOTHING is enough and no
    locking upsert is needed.
    """
    svc = service(engine, migrated_db)
    user_id = await provision(engine)
    factory = create_session_factory(engine)

    async with factory() as rival:
        rival.add(Experiment(user_id=user_id, experiment_key=KEY, variant=VARIANT_VIDEO))
        await rival.flush()

        racer = asyncio.create_task(svc.assign(user_id, experiment_key=KEY))
        await asyncio.sleep(0.25)
        assert not racer.done(), "the racer should be blocked on the rival's pending insert"

        await rival.commit()
        assert await racer == VARIANT_VIDEO

    assert len(await rows(engine, user_id)) == 1


# ---------------------------------------------------------------- switched off


async def test_an_experiment_that_is_off_writes_nothing(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    """A row for a learner who was never exposed to the experiment is a phantom
    in the denominator, and afterwards it is indistinguishable from a real one."""
    user_id = await provision(engine)

    off = service(engine, migrated_db, EXPERIMENT_EXPLAIN_MEDIA_ENABLED="false")

    assert await off.assign(user_id, experiment_key=KEY) is None
    assert await rows(engine, user_id) == []


async def test_switching_off_stops_the_experiment_for_assigned_learners(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    """Off means off. Returning the stored arm would keep holding video back
    from a Pro learner with the experiment visibly disabled, and no way to tell
    why. The row survives for the analysis."""
    user_id = await provision(engine)
    await service(engine, migrated_db).assign(user_id, experiment_key=KEY)

    off = service(engine, migrated_db, EXPERIMENT_EXPLAIN_MEDIA_ENABLED="false")

    assert await off.assign(user_id, experiment_key=KEY) is None
    assert len(await rows(engine, user_id)) == 1


async def test_turning_it_back_on_restores_the_same_arm(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    svc = service(engine, migrated_db)
    user_id = await provision(engine)
    before = await svc.assign(user_id, experiment_key=KEY)

    off = service(engine, migrated_db, EXPERIMENT_EXPLAIN_MEDIA_ENABLED="false")
    await off.assign(user_id, experiment_key=KEY)

    assert await svc.assign(user_id, experiment_key=KEY) == before


# ------------------------------------------------------------------ bad input


async def test_an_unregistered_experiment_is_refused(engine: AsyncEngine, migrated_db: URL) -> None:
    user_id = await provision(engine)

    with pytest.raises(UnknownExperimentError, match="no experiment named 'streak_nudge'"):
        await service(engine, migrated_db).assign(user_id, experiment_key="streak_nudge")

    assert await rows(engine, user_id) == []


async def test_assigning_a_user_that_does_not_exist_is_refused(
    engine: AsyncEngine, migrated_db: URL
) -> None:
    """The foreign key catches it. An assignment for a nonexistent learner would
    sit in the table unattached and inflate one arm's denominator."""
    with pytest.raises(sa.exc.IntegrityError, match="experiments_user_id_fkey"):
        await service(engine, migrated_db).assign(uuid.uuid4(), experiment_key=KEY)
