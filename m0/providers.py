"""Where the M0 scripts get their providers, and where their spending is written.

**Providers.** TTS comes from the application registry, Azure included, since
E8 moved it there (D-137). Scoring's ``azure`` still resolves to the
verification adapter in ``m0.azure`` until E9 moves that too (D-126); every
other name goes to the application registries untouched.

**Spending.** CLAUDE.md section 8: a call that is not in ``cost_ledger`` did
not happen. A billed run therefore opens the ledger *before* its first
request and refuses to start if it cannot — finding out that Postgres is down
after thirty paid requests would leave thirty calls nobody can account for.
A fake run makes no external call, so it records nothing and needs no
database, which is what lets the scripts and their tests run without one.

Rows go in as ``content_production``: one-off spend with no learner behind it,
which PRD 11.3 keeps out of per-user operating cost. That is what M0 is, and
adding a ledger category to the application for a temporary tool would be a
change to accepted code made for the tool's convenience.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Final, Protocol

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import Settings
from app.core.db import create_engine, create_session_factory
from app.services.cost_ledger import CostLedger, LedgerRef, PendingEntry
from app.services.scoring import registry as scoring_registry
from app.services.scoring.base import Language, PronunciationScorer
from app.services.tts import registry as tts_registry
from app.services.tts.base import TTSProvider, Voice
from m0.azure import AzureScorer

LEDGER_REF: Final[LedgerRef] = "content_production"


def build_tts(
    name: str, *, settings: Settings, voices: Mapping[Voice, str] | None = None
) -> TTSProvider:
    # Azure TTS is in the application registry now (E8, D-137).
    return tts_registry.build_tts(name, settings=settings, voices=voices)


def get_scorer(language: Language, *, settings: Settings) -> PronunciationScorer:
    if settings.scoring_provider == AzureScorer.name:
        return AzureScorer.from_settings(settings)
    return scoring_registry.get_scorer(language, settings=settings)


class Ledger(Protocol):
    """What the scripts need from a ledger: ``CostLedger``'s one method."""

    def external_call(
        self, *, provider: str, ref: LedgerRef, user_id: uuid.UUID | None = None
    ) -> AbstractAsyncContextManager[PendingEntry]: ...


class Unbilled:
    """For a run where nothing external is called. Records go nowhere."""

    @asynccontextmanager
    async def external_call(
        self, *, provider: str, ref: LedgerRef, user_id: uuid.UUID | None = None
    ) -> AsyncIterator[PendingEntry]:
        yield PendingEntry(provider=provider, ref=ref, user_id=user_id)


class LedgerUnavailableError(RuntimeError):
    """A billed run could not reach cost_ledger, so it did not start."""


@asynccontextmanager
async def open_ledger(settings: Settings, *, billed: bool) -> AsyncIterator[Ledger]:
    """The real ledger for a billed run, after proving it can be written to.

    Raises:
        LedgerUnavailableError: the database is down or not migrated. Raised before
            any request is made.
    """
    if not billed:
        yield Unbilled()
        return

    engine = create_engine(settings)
    try:
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1 FROM cost_ledger LIMIT 0"))
        except (SQLAlchemyError, OSError) as error:
            raise LedgerUnavailableError(
                f"cannot reach cost_ledger ({type(error).__name__}); start Postgres and run "
                "`make migrate`. No request was made (CLAUDE.md section 8)."
            ) from error
        yield CostLedger(session_factory=create_session_factory(engine), settings=settings)
    finally:
        await engine.dispose()


__all__ = [
    "LEDGER_REF",
    "Ledger",
    "LedgerUnavailableError",
    "Unbilled",
    "build_tts",
    "get_scorer",
    "open_ledger",
]
