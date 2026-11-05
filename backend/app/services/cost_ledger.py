"""Every external API call leaves a row, or the call is treated as a defect.

CLAUDE.md section 8 is blunt about it: an unledgered call counts as not having
happened. That rule only holds if forgetting is noisy, so ``external_call`` is a
context manager that raises when its body finishes without recording anything.
The declaration and the enforcement are the same object — you cannot wrap a call
for accounting and then quietly not account for it.

Two properties are worth spelling out because both are easy to get wrong:

Rows are written in their own session, not the caller's. The money left the
account whether or not the surrounding business transaction commits, and an
attempt that fails after a paid assessment must still show the cost. Joining the
caller's transaction would delete exactly the records that matter most — the
ones from a request that went wrong.

Recorded costs are persisted even when the body then raises. The call already
happened; an exception afterwards is a separate problem and must not erase the
evidence of spending.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Final, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.models.commerce import CostLedger as CostLedgerRow

log = get_logger(__name__)

#: The units DATA_MODEL.sql names for cost_ledger.unit. A Literal rather than a
#: free string so a typo is a type error instead of an unaggregatable row.
LedgerUnit = Literal["seconds", "tokens", "calls", "minutes"]

#: What the spend was for. 'content_production' is kept out of per-user
#: operating cost (PRD 11.3) — pack builds are one-off, not a monthly burden.
#:
#: 'payment' rows are written with a cost of zero, on purpose (D-075). The
#: acquirer's fee is a percentage of the transaction, so charging it to the
#: learner who paid it would mean that subscribing moves someone closer to
#: their own cost ceiling — a learner throttled for having paid us. What the
#: row is for is the other half of CLAUDE.md section 8: a call that happened
#: leaves a trace, so the call count, the failure rate and which channel is
#: flaky are all visible in /admin/costs rather than only in the logs.
LedgerRef = Literal["attempt", "realtime", "content_production", "payment"]


class UnledgeredCallError(RuntimeError):
    """An external call completed without recording what it cost.

    A programming error, not a business rule: someone wrapped a call for
    accounting and then did not account for it (CODING_STANDARDS 5.1, case 3).
    """


#: Thousandths of a US cent in a cent. The unit costs are summed in (D-136).
MILLICENTS_PER_CENT: Final = 1000


def millicents_to_cents(millicents: int) -> int:
    """Rounded up: a total of 0.4 cents still cost something, and the ceiling
    is checked against what was spent, so the rounding goes against us."""
    return -(-millicents // MILLICENTS_PER_CENT)


@dataclass(frozen=True)
class RecordedCost:
    """One line of spending, held in thousandths of a cent."""

    unit: LedgerUnit
    quantity: float
    cost_usd_millicents: int

    @property
    def cost_usd_cents(self) -> int:
        """This line alone, rounded up — for reading a row, never for adding rows up."""
        return millicents_to_cents(self.cost_usd_millicents)


@dataclass
class PendingEntry:
    """Handed to the body of ``external_call`` so it can report what it spent."""

    provider: str
    ref: LedgerRef
    user_id: uuid.UUID | None = None
    costs: list[RecordedCost] = field(default_factory=list)

    def record(
        self,
        *,
        unit: LedgerUnit,
        quantity: float,
        cost_usd_cents: int | None = None,
        cost_usd_millicents: int | None = None,
    ) -> None:
        """Report one unit of spend, in whole cents or in thousandths of one.

        Exactly one of the two. Thousandths are for calls that cost a fraction
        of a cent each — a pronunciation assessment — where reporting whole
        cents would round every call up and overstate a month tenfold (D-136).
        Call it more than once for a batch.

        Raises:
            TypeError: cost is not an int, or both or neither were given. R4
                applies to either unit — and bool slips past a plain isinstance.
            ValueError: a negative quantity or cost, which would silently pull
                down whatever aggregate reads this table.
        """
        if (cost_usd_cents is None) == (cost_usd_millicents is None):
            raise TypeError("give exactly one of cost_usd_cents and cost_usd_millicents")
        name, value = (
            ("cost_usd_cents", cost_usd_cents)
            if cost_usd_cents is not None
            else ("cost_usd_millicents", cost_usd_millicents)
        )
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{name} must be int, got {type(value).__name__}; float money is a bug")
        if value < 0:
            raise ValueError(f"{name} must be >= 0, got {value}")
        if quantity < 0:
            raise ValueError(f"quantity must be >= 0, got {quantity}")

        millicents = value * MILLICENTS_PER_CENT if cost_usd_cents is not None else value
        self.costs.append(
            RecordedCost(unit=unit, quantity=float(quantity), cost_usd_millicents=millicents)
        )

    @property
    def total_usd_millicents(self) -> int:
        return sum(cost.cost_usd_millicents for cost in self.costs)

    @property
    def total_usd_cents(self) -> int:
        """The whole entry, summed first and rounded once."""
        return millicents_to_cents(self.total_usd_millicents)


class CostLedger:
    """Writes cost_ledger rows and refuses to let a call go unrecorded."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], AsyncSession],
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings

    @asynccontextmanager
    async def external_call(
        self, *, provider: str, ref: LedgerRef, user_id: uuid.UUID | None = None
    ) -> AsyncIterator[PendingEntry]:
        """Wrap one external call so its cost cannot go unrecorded.

            async with ledger.external_call(provider=scorer.name, ref="attempt",
                                            user_id=user.id) as entry:
                result = await scorer.assess(audio, ...)
                entry.record(unit="calls", quantity=1,
                             cost_usd_cents=result.cost_usd_cents)

        Raises:
            UnledgeredCallError: the body finished without calling record(), and
                COST_LEDGER_REQUIRED is on. With it off this only warns, which
                is why CONFIG_REFERENCE says dev and CI must leave it on.
        """
        pending = PendingEntry(provider=provider, ref=ref, user_id=user_id)
        try:
            yield pending
            if not pending.costs:
                self._report_unledgered(provider, ref)
        finally:
            # Also runs while an exception propagates: a call that was recorded
            # and then failed downstream still spent money.
            if pending.costs:
                await self._persist(pending)

    async def _persist(self, pending: PendingEntry) -> None:
        """Commit in a session of its own. See the module docstring."""
        async with self._session_factory() as session:
            session.add_all(
                [
                    CostLedgerRow(
                        user_id=pending.user_id,
                        provider=pending.provider,
                        unit=cost.unit,
                        quantity=cost.quantity,
                        cost_usd_cents_est=cost.cost_usd_cents,
                        cost_usd_millicents_est=cost.cost_usd_millicents,
                        ref=pending.ref,
                    )
                    for cost in pending.costs
                ]
            )
            await session.commit()

        log.info(
            "cost_ledger.recorded",
            provider=pending.provider,
            ref=pending.ref,
            entries=len(pending.costs),
            cost_usd_cents=pending.total_usd_cents,
        )

    def _report_unledgered(self, provider: str, ref: LedgerRef) -> None:
        message = (
            f"external call to {provider!r} for {ref!r} finished without recording a cost; "
            "call entry.record(...) inside the external_call block "
            "(CLAUDE.md section 8)"
        )
        if self._settings.cost_ledger_required:
            raise UnledgeredCallError(message)
        log.warning("cost_ledger.unledgered_call", provider=provider, ref=ref)


__all__ = [
    "MILLICENTS_PER_CENT",
    "CostLedger",
    "LedgerRef",
    "LedgerUnit",
    "PendingEntry",
    "RecordedCost",
    "UnledgeredCallError",
    "millicents_to_cents",
]
