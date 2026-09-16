"""Which concepts come back next, and how many fit in the time a learner has.

PRD 3.2: the queue is ordered by mastery ascending, then by how overdue the
concept is. Weakest first, because the point of Smart Review is to spend a
learner's limited attention where it is worth the most.

Only concepts that are actually due appear. Including everything sorted by
mastery would surface a concept attempted five minutes ago — its mastery is
still low — and that is spaced repetition switched off. The schedule decides
*when*; this decides *what*, among the things the schedule already released.

The per-concept time estimate is an input, not something computed here. How long
a review takes depends on how many drill and vocab items the concept carries,
which lives in lesson_items and is not the domain layer's to join. The caller
supplies it; today that will be a flat figure, and it can become a real estimate
without this module changing.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

#: Durations a learner can pick (BACKLOG C3). A product decision about what the
#: interface offers, not a number live data would move, so it is a constant
#: rather than configuration — offering 30 minutes means designing for it, not
#: editing an env file.
REVIEW_DURATION_CHOICES: Final[tuple[int, ...]] = (5, 10, 15, 25)

_SECONDS_PER_MINUTE: Final = 60

#: Sort position for a concept with no schedule at all. Earliest possible, so
#: it sorts ahead of anything with a real date: never having been scheduled is
#: the most neglected a concept can be, not the least.
_NEVER_SCHEDULED: Final = datetime.datetime.min.replace(tzinfo=datetime.UTC)


@dataclass(frozen=True)
class ReviewCandidate:
    """One concept_mastery row, as far as the queue cares."""

    concept_id: uuid.UUID
    mastery: float
    #: None means attempted but never scheduled, which should not happen once
    #: D3 always writes a schedule — treated as due so it surfaces rather than
    #: staying invisible forever.
    next_due_at: datetime.datetime | None
    estimated_seconds: int

    def is_due(self, now: datetime.datetime) -> bool:
        return self.next_due_at is None or self.next_due_at <= now


@dataclass(frozen=True)
class ReviewQueue:
    """What to review now, and what did not fit."""

    entries: tuple[ReviewCandidate, ...]
    requested_minutes: int
    #: Due concepts left out for lack of time. Lets a client say "12 more
    #: waiting" instead of implying the learner is finished.
    skipped: int

    @property
    def estimated_seconds(self) -> int:
        return sum(entry.estimated_seconds for entry in self.entries)

    @property
    def is_empty(self) -> bool:
        return not self.entries

    @property
    def over_budget(self) -> bool:
        """True when a single concept was longer than the whole slot.

        The queue includes it anyway; see build_review_queue.
        """
        return self.estimated_seconds > self.requested_minutes * _SECONDS_PER_MINUTE


def due_candidates(
    candidates: Sequence[ReviewCandidate], *, now: datetime.datetime
) -> list[ReviewCandidate]:
    """Everything the schedule has released, weakest and most overdue first.

    The final sort key is the concept id. Without it two concepts with equal
    mastery and the same due time could come back in either order, and a learner
    refreshing the screen would watch the list reshuffle.
    """
    _require_aware(now)
    due = [candidate for candidate in candidates if candidate.is_due(now)]
    return sorted(
        due,
        key=lambda candidate: (
            candidate.mastery,
            candidate.next_due_at or _NEVER_SCHEDULED,
            str(candidate.concept_id),
        ),
    )


def build_review_queue(
    candidates: Sequence[ReviewCandidate],
    *,
    minutes: int,
    now: datetime.datetime,
) -> ReviewQueue:
    """Fill the learner's chosen slot with the concepts that need it most.

    The budget is a target rather than a ceiling: if the first concept alone is
    longer than the slot it is still returned. A learner with twenty overdue
    concepts being told there is nothing to review, because each takes six
    minutes and they picked five, is a worse answer than one slightly long
    session (docs/DECISIONS.md D-017).

    Raises:
        ValueError: a duration the interface does not offer, or a naive ``now``.
    """
    if minutes not in REVIEW_DURATION_CHOICES:
        raise ValueError(
            f"{minutes} is not an offered review duration; choices are "
            f"{list(REVIEW_DURATION_CHOICES)}"
        )

    ordered = due_candidates(candidates, now=now)
    budget = minutes * _SECONDS_PER_MINUTE

    entries: list[ReviewCandidate] = []
    spent = 0
    for candidate in ordered:
        if entries and spent + candidate.estimated_seconds > budget:
            break
        entries.append(candidate)
        spent += candidate.estimated_seconds

    return ReviewQueue(
        entries=tuple(entries),
        requested_minutes=minutes,
        skipped=len(ordered) - len(entries),
    )


def _require_aware(now: datetime.datetime) -> None:
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError(
            "now must be timezone-aware; comparing it against a stored "
            "next_due_at would otherwise raise or silently compare wrong"
        )
