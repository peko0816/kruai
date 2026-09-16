"""How one spoken attempt moves a concept's mastery (PRD 9.2).

    delta   = (pron_score - SCORING_PASS_THRESHOLD) / MASTERY_DELTA_BASE
    mastery = clamp(mastery + delta * weight[item_type], 0, 100)

Pure functions over values. Nothing here touches the database or a provider,
which is what lets the whole of the product's central judgement — did this
learner get better — be tested in milliseconds.

Two properties of the formula are worth stating, because both look like bugs
until you see they are deliberate:

The scale is asymmetric. With the shipped defaults a perfect attempt earns
+1.0 and a silent one costs -1.5, because failing to produce a sentence is
stronger evidence than producing it once. Nothing enforces that asymmetry — it
falls out of the pass mark sitting at 60 rather than at 50.

The steps are small. Weight never exceeds 1.0, so a single attempt moves
mastery by at most one point and a concept takes on the order of a hundred good
attempts to reach the ceiling from zero. That is the spec as written; whether it
is the right pace is a question for M0-1's real score distribution, which is why
every term in the formula is configuration rather than a literal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from app.core.config import Settings

#: Item types that produce an attempt. 'explain' is a lecture card — there is
#: nothing to say back to it, so it never reaches this module and has no weight
#: in PRD 9.2's table.
ScorableItemType = Literal["drill", "vocab", "qa"]

#: The bounds of the mastery scale, not thresholds. MASTERY_HIGH and MASTERY_LOW
#: are tunable and live in configuration; 0 and 100 define what the number means
#: at all, and changing them would reinterpret every value already stored.
MASTERY_FLOOR: Final = 0.0
MASTERY_CEILING: Final = 100.0

#: Range a provider is contractually allowed to report (scoring/base.py).
_SCORE_FLOOR: Final = 0.0
_SCORE_CEILING: Final = 100.0


@dataclass(frozen=True)
class MasteryUpdate:
    """The result of applying one attempt, with enough detail to explain itself.

    The intermediate values are kept because a mastery score that moved
    unexpectedly is nearly impossible to diagnose after the fact — the inputs
    are gone and the formula has four terms.
    """

    previous: float
    #: Before weighting, so a log line shows the raw quality of the attempt.
    delta: float
    weight: float
    current: float
    #: True when the clamp bit. A learner pinned at the ceiling looks identical
    #: to one making no progress unless this is recorded.
    clamped: bool

    @property
    def weighted_delta(self) -> float:
        return self.delta * self.weight

    @property
    def improved(self) -> bool:
        return self.current > self.previous


def weight_for(item_type: str, *, settings: Settings) -> float:
    """Weight for an item type — closer to real use means a heavier weight.

    Raises:
        ValueError: not a scorable type. 'explain' lands here if an attempt is
            ever recorded against a lecture card, which the API layer should
            have refused; anything else is a typo or a new item type that PRD
            9.2's table has not been extended to cover.
    """
    weights = {
        "drill": settings.mastery_weight_drill,
        "vocab": settings.mastery_weight_vocab,
        "qa": settings.mastery_weight_qa,
    }
    try:
        return weights[item_type]
    except KeyError:
        raise ValueError(
            f"{item_type!r} has no mastery weight; scorable types are {sorted(weights)}"
        ) from None


def score_delta(pron_score: float, *, settings: Settings) -> float:
    """Signed progress from one score, before weighting.

    Zero exactly at the pass mark: an attempt that just passes is evidence of
    neither improvement nor decay.

    Raises:
        ValueError: the score is outside 0-100, which scoring/base.py forbids.
            Letting it through would not break the clamp, but it would let one
            malformed response move mastery by twenty points in a single step,
            and nothing downstream would look wrong.
    """
    if not _SCORE_FLOOR <= pron_score <= _SCORE_CEILING:
        raise ValueError(
            f"pron_score must be within {_SCORE_FLOOR}-{_SCORE_CEILING}, got {pron_score}"
        )
    return (pron_score - settings.scoring_pass_threshold) / settings.mastery_delta_base


def is_passing(pron_score: float, *, settings: Settings) -> bool:
    """Whether this attempt counts as passed, for attempts.passed.

    Shares SCORING_PASS_THRESHOLD with score_delta on purpose. Computing it
    separately in the API layer would let the two drift, and an attempt marked
    passed while its mastery went down is the kind of contradiction nobody
    thinks to look for.
    """
    return pron_score >= settings.scoring_pass_threshold


def clamp_mastery(value: float) -> float:
    return max(MASTERY_FLOOR, min(MASTERY_CEILING, value))


def apply_attempt(
    *,
    current_mastery: float,
    pron_score: float,
    item_type: str,
    settings: Settings,
) -> MasteryUpdate:
    """Apply one attempt to a concept's mastery.

    Args:
        current_mastery: the stored score; 0 for a concept never attempted.
        pron_score: the provider's composite score, 0-100.
        item_type: 'drill', 'vocab' or 'qa'.

    Raises:
        ValueError: the score is out of range, the item type is not scorable, or
            the current mastery is itself outside the scale — the last means
            something already corrupted the stored value, and continuing would
            bury the evidence under a fresh clamp.
    """
    if not MASTERY_FLOOR <= current_mastery <= MASTERY_CEILING:
        raise ValueError(
            f"current_mastery must be within {MASTERY_FLOOR}-{MASTERY_CEILING}, "
            f"got {current_mastery}"
        )

    delta = score_delta(pron_score, settings=settings)
    weight = weight_for(item_type, settings=settings)
    unclamped = current_mastery + delta * weight
    current = clamp_mastery(unclamped)

    return MasteryUpdate(
        previous=current_mastery,
        delta=delta,
        weight=weight,
        current=current,
        clamped=current != unclamped,
    )
