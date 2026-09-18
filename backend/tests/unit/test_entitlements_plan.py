"""Which plan wins when a learner holds more than one live subscription.

The rule is small and the reason it is a rule at all is the upgrade path: buying
Pro halfway through a Basic month leaves both rows alive, and picking the wrong
one silently downgrades somebody who just paid.
"""

from __future__ import annotations

import pytest

from app.core.config import PLAN_RANK, plan_rank
from app.services.entitlements import ENTITLING_STATUSES, FREE_PLAN, best_plan


def test_no_subscription_means_free() -> None:
    assert best_plan([]) == FREE_PLAN


@pytest.mark.parametrize("plan", ["free", "basic", "pro"])
def test_a_single_subscription_is_the_answer(plan: str) -> None:
    assert best_plan([plan]) == plan


@pytest.mark.parametrize(
    ("held", "expected"),
    [
        (["basic", "pro"], "pro"),
        (["pro", "basic"], "pro"),
        (["free", "basic"], "basic"),
        (["free", "pro", "basic"], "pro"),
        (["basic", "basic"], "basic"),
    ],
)
def test_the_best_live_plan_wins(held: list[str], expected: str) -> None:
    """An upgrade leaves both rows alive until the old period ends."""
    assert best_plan(held) == expected


def test_order_of_arrival_does_not_matter() -> None:
    assert best_plan(["pro", "free"]) == best_plan(["free", "pro"])


def test_an_unknown_plan_is_refused() -> None:
    """Ranking it lowest would downgrade a paying learner with nothing failing."""
    with pytest.raises(ValueError, match="unknown plan 'platinum'"):
        best_plan(["basic", "platinum"])


# --------------------------------------------------------------------- the ladder


def test_the_ladder_is_cheapest_first() -> None:
    assert list(PLAN_RANK) == ["free", "basic", "pro"]
    assert plan_rank("free") < plan_rank("basic") < plan_rank("pro")


def test_grace_still_entitles() -> None:
    """ARCHITECTURE 3.4: benefits continue through the grace window.

    A learner whose renewal is a day late keeps the lesson they are in; the
    downgrade happens when grace ends, in the renewal task, not here.
    """
    assert set(ENTITLING_STATUSES) == {"active", "grace"}


@pytest.mark.parametrize("status", ["expired", "cancelled"])
def test_a_dead_subscription_does_not_entitle(status: str) -> None:
    assert status not in ENTITLING_STATUSES
