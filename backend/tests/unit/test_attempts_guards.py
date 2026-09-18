"""What the attempts endpoint will accept as a score.

Split out of the handler so it can be tested against results no fake produces.
FakeScorer sets ``ok=False`` and leaves ``pron_score`` empty together, so an
integration test cannot tell the two conditions apart — and the D3 mutation
that dropped the ``ok`` check survived the whole suite because of it.
"""

from __future__ import annotations

import pytest

from app.api.v1.attempts import usable_score
from app.services.scoring.base import PronunciationResult


def test_a_scored_result_is_usable() -> None:
    assert usable_score(PronunciationResult(ok=True, pron_score=72.0)) == 72.0


def test_a_reported_failure_has_no_score() -> None:
    assert usable_score(PronunciationResult(ok=False, error_code="scoring.timeout")) is None


def test_a_failure_carrying_a_score_is_still_a_failure() -> None:
    """The provider's own verdict outranks anything else in the payload.

    A vendor that returned a partial score alongside ok=False would otherwise
    cost the learner an attempt and move their mastery by a number the provider
    has disowned.
    """
    disowned = PronunciationResult(ok=False, pron_score=88.0, error_code="scoring.partial")

    assert usable_score(disowned) is None


def test_a_success_with_no_score_is_unusable() -> None:
    """Nothing for mastery to apply, whatever the provider called it."""
    assert usable_score(PronunciationResult(ok=True, pron_score=None)) is None


@pytest.mark.parametrize("score", [0.0, 100.0])
def test_the_extremes_of_the_scale_are_scores(score: float) -> None:
    """Zero is a score, not a missing one — falsiness must not decide this."""
    assert usable_score(PronunciationResult(ok=True, pron_score=score)) == score
