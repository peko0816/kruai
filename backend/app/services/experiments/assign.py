"""Recording an assignment, once, for as long as the experiment runs.

Bucketing is deterministic, so persisting it looks redundant — the variant can
always be recomputed from the id. It is not redundant, and the reason is the
whole point of the table: **the stored row outranks the calculation.**

Move EXPERIMENT_EXPLAIN_MEDIA_SPLIT from 0.5 to 0.7 halfway through the run and
a recompute-every-time implementation silently migrates a fifth of the learners
from audio to video. They carry their earlier sessions with them. Completion
rates are then averages over people who were in both arms, the 15% threshold in
PRD 7.2 is measured against a sample that does not exist, and nothing anywhere
reports a problem. The same applies to changing the hash, adding a plan, or
fixing a bug in this file. Once a learner has been assigned, the row is the
answer.

This module holds a session factory, which the domain layer is not supposed to
do (ARCHITECTURE section 1). It is a narrower exception than D-018's: there the
decision itself had to be a SQL statement, so nothing could be extracted. Here
only the writing is IO — every rule about *which* arm lives in bucket.py, pure
and fully unit-tested. See docs/DECISIONS.md D-023.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.models.learning import Experiment
from app.services.experiments.bucket import (
    ExperimentDefinition,
    bucket_fraction,
    choose_variant,
    definition,
)

log = get_logger(__name__)


class Experiments:
    """Stable A/B assignment, backed by the experiments table."""

    def __init__(self, *, session_factory: Callable[[], AsyncSession], settings: Settings) -> None:
        self._session_factory = session_factory
        self._settings = settings

    async def assign(self, user_id: uuid.UUID, *, experiment_key: str) -> str | None:
        """This learner's arm, assigning them on first ask.

        Idempotent and safe to call on every request: a learner already in the
        table keeps the arm they have, whatever the configuration now says.

        Returns:
            The variant, or None when the experiment is switched off — in which
            case nothing is read and nothing is written. Rows created while an
            experiment is off would be assignments nobody was ever exposed to,
            and they are indistinguishable afterwards from real ones.

        Raises:
            UnknownExperimentError: no definition for this key.
        """
        experiment = definition(experiment_key)
        variant = choose_variant(user_id, experiment=experiment, settings=self._settings)
        if variant is None:
            return None

        async with self._session_factory() as session:
            existing = await self._stored(session, user_id, experiment)
            if existing is not None:
                return existing

            # DO NOTHING rather than a locking upsert. Two concurrent requests
            # for one learner compute the same variant, so a lost race loses
            # nothing — unlike a quota counter, where the same shape of race is
            # the bug entitlements/quota.py exists to prevent.
            statement = (
                pg_insert(Experiment)
                .values(user_id=user_id, experiment_key=experiment.key, variant=variant)
                .on_conflict_do_nothing(index_elements=["user_id", "experiment_key"])
                .returning(Experiment.variant)
            )
            inserted: str | None = (await session.execute(statement)).scalar_one_or_none()
            await session.commit()

            if inserted is None:
                # Another request inserted first, possibly under a different
                # split if a deploy is mid-flight. Their row is the answer.
                stored = await self._stored(session, user_id, experiment)
                if stored is None:  # pragma: no cover - would mean a vanished row
                    raise RuntimeError(
                        f"assignment for user {user_id} on {experiment.key!r} "
                        "conflicted but could not be read back"
                    )
                return stored

        log.info(
            "experiments.assigned",
            user_id=str(user_id),
            experiment_key=experiment.key,
            variant=inserted,
            # Logged so an assignment can be checked without rerunning the hash,
            # and so a split that drifts from its configuration is visible.
            bucket=bucket_fraction(user_id, experiment_key=experiment.key),
        )
        return inserted

    async def _stored(
        self, session: AsyncSession, user_id: uuid.UUID, experiment: ExperimentDefinition
    ) -> str | None:
        row = await session.get(Experiment, {"user_id": user_id, "experiment_key": experiment.key})
        return None if row is None else row.variant
