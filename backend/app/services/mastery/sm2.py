"""When a concept comes back (PRD 9.2's SM-2 variant).

    mastery >= MASTERY_HIGH  ->  interval *= ease_factor
    mastery <  MASTERY_LOW   ->  interval = 1 day, ease_factor -= SM2_EASE_PENALTY
    next_due_at = now + interval

This is a variant, not SM-2 proper, and the difference is deliberate: ease only
ever decays. Classic SM-2 raises it on good recall, so a learner can earn back
the spacing they lost. Here a concept that was once failed keeps its reduced
ease permanently, which over a long enough run pulls every concept toward
SM2_EASE_MIN. PRD 9.2 specifies it that way and this implements it as written —
see docs/DECISIONS.md D-015 for the band it leaves undefined.

Nothing here reads a clock. ``now`` is passed in, because a scheduler that calls
datetime.now() internally can only be tested by waiting.
"""

from __future__ import annotations

import datetime
import math
from dataclasses import dataclass
from typing import Final, Literal

from app.core.config import Settings
from app.services.mastery.delta import MASTERY_CEILING, MASTERY_FLOOR

#: What happened to the schedule. Recorded because "why is this concept due
#: again tomorrow" is otherwise unanswerable from the stored row alone.
ScheduleBand = Literal["extended", "held", "reset"]

#: Where a concept starts, and where a failure sends it back to.
INITIAL_INTERVAL_DAYS: Final = 1

#: An ease at or below this cannot grow an interval at all, so a learner would
#: review the same concept every day forever. Config already enforces
#: SM2_EASE_MIN >= 1.0; this is the same boundary named for the reader.
_NO_GROWTH_EASE: Final = 1.0


@dataclass(frozen=True)
class ReviewSchedule:
    """The scheduling half of a concept_mastery row after one review."""

    ease_factor: float
    interval_days: int
    next_due_at: datetime.datetime
    band: ScheduleBand

    @property
    def ease_decayed(self) -> bool:
        return self.band == "reset"


def initial_ease_factor(settings: Settings) -> float:
    """Ease for a concept nobody has attempted yet.

    Exists so callers read SM2_EASE_INITIAL rather than letting the database
    default supply it. concept_mastery.ease_factor defaults to 2.5 in the DDL,
    which matches today's configured value and would silently stop matching if
    anyone changed the setting.

    Resolved in D3 (was constraint L-6): api/v1/attempts.py seeds every new row
    with this function, and
    test_a_new_row_starts_from_the_configured_ease_not_the_column_default
    configures a different initial so the two cannot be confused. A future
    writer of concept_mastery must do the same.
    """
    return settings.sm2_ease_initial


def _grow(interval_days: int, ease_factor: float) -> int:
    """Next interval, rounded up.

    Rounding up rather than down or to nearest is not a stylistic choice. At the
    minimum ease of 1.3, ``floor(1 * 1.3)`` and ``round(1 * 1.3)`` are both 1, so
    a learner whose ease had decayed would review that concept every single day
    forever — while passing it. Rounding up is the only one of the three that
    always moves. Past the first few reviews the three differ by a day, which is
    noise against intervals measured in weeks.
    """
    return math.ceil(interval_days * ease_factor)


def schedule_review(
    *,
    mastery: float,
    ease_factor: float,
    interval_days: int,
    now: datetime.datetime,
    settings: Settings,
) -> ReviewSchedule:
    """Decide when this concept should come back.

    Args:
        mastery: the score after applying the attempt, 0-100.
        ease_factor: the stored multiplier; initial_ease_factor() for a new concept.
        interval_days: the stored interval; INITIAL_INTERVAL_DAYS for a new one.
        now: timezone-aware, in UTC. Passed in so this stays testable.

    Raises:
        ValueError: any input outside its documented range, or a naive ``now``.
            A naive datetime would be stored as if it were in whatever timezone
            the session happened to use, which surfaces months later as reviews
            arriving at the wrong time for one region only.
    """
    _validate(mastery=mastery, ease_factor=ease_factor, interval_days=interval_days, now=now)

    if mastery >= settings.mastery_high:
        band: ScheduleBand = "extended"
        next_ease = ease_factor
        next_interval = _grow(interval_days, ease_factor)
    elif mastery < settings.mastery_low:
        band = "reset"
        next_ease = max(settings.sm2_ease_min, ease_factor - settings.sm2_ease_penalty)
        next_interval = INITIAL_INTERVAL_DAYS
    else:
        # Neither rule in PRD 9.2 fires between the two thresholds, so nothing
        # changes and the concept simply comes round again on its current
        # spacing (docs/DECISIONS.md D-015).
        band = "held"
        next_ease = ease_factor
        next_interval = interval_days

    return ReviewSchedule(
        ease_factor=next_ease,
        interval_days=next_interval,
        next_due_at=now + datetime.timedelta(days=next_interval),
        band=band,
    )


def _validate(
    *, mastery: float, ease_factor: float, interval_days: int, now: datetime.datetime
) -> None:
    if not MASTERY_FLOOR <= mastery <= MASTERY_CEILING:
        raise ValueError(f"mastery must be within {MASTERY_FLOOR}-{MASTERY_CEILING}, got {mastery}")
    if ease_factor < _NO_GROWTH_EASE:
        raise ValueError(
            f"ease_factor must be at least {_NO_GROWTH_EASE}, got {ease_factor}; "
            "below it an interval would shrink on every success"
        )
    if interval_days < INITIAL_INTERVAL_DAYS:
        raise ValueError(
            f"interval_days must be at least {INITIAL_INTERVAL_DAYS}, got {interval_days}"
        )
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError(
            "now must be timezone-aware; a naive datetime lands in next_due_at as if "
            "it were in whatever timezone the session used"
        )
