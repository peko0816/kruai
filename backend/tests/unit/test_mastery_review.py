"""Queue ordering, which is the whole of Smart Review's value.

BACKLOG C3's acceptance is a mastery distribution producing an expected order,
so most of this is exactly that. Two properties get extra attention because
neither shows up as an error:

Determinism. Without a final tie-break, two concepts with equal mastery and the
same due time come back in whatever order the input happened to be in, and a
learner refreshing watches the list reshuffle.

Not-yet-due exclusion. Including everything sorted by mastery would surface a
concept attempted five minutes ago, since its mastery is still low — spaced
repetition switched off, with the queue still looking perfectly sensible.
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from app.services.mastery import (
    REVIEW_DURATION_CHOICES,
    ReviewCandidate,
    build_review_queue,
    due_candidates,
)

NOW = datetime.datetime(2026, 9, 16, 12, 0, tzinfo=datetime.UTC)

# Stable ids so ordering assertions are readable and the tie-break is testable.
IDS = [uuid.UUID(int=index) for index in range(1, 12)]


def candidate(
    index: int,
    mastery: float,
    *,
    due_hours_ago: float | None = 1.0,
    seconds: int = 60,
) -> ReviewCandidate:
    due = None if due_hours_ago is None else NOW - datetime.timedelta(hours=due_hours_ago)
    return ReviewCandidate(
        concept_id=IDS[index],
        mastery=mastery,
        next_due_at=due,
        estimated_seconds=seconds,
    )


def order(queue_entries: tuple[ReviewCandidate, ...] | list[ReviewCandidate]) -> list[int]:
    return [IDS.index(entry.concept_id) for entry in queue_entries]


# ------------------------------------------------------------------- ordering


def test_the_weakest_concept_comes_first() -> None:
    """BACKLOG C3 acceptance: a given mastery distribution orders as expected."""
    given = [candidate(0, 70.0), candidate(1, 20.0), candidate(2, 45.0), candidate(3, 95.0)]
    assert order(due_candidates(given, now=NOW)) == [1, 2, 0, 3]


def test_equal_mastery_puts_the_most_overdue_first() -> None:
    given = [
        candidate(0, 50.0, due_hours_ago=1),
        candidate(1, 50.0, due_hours_ago=72),
        candidate(2, 50.0, due_hours_ago=24),
    ]
    assert order(due_candidates(given, now=NOW)) == [1, 2, 0]


def test_mastery_outranks_being_overdue() -> None:
    """A concept a month late but nearly mastered still waits behind a weak one
    due an hour ago — attention goes where it is worth most."""
    given = [
        candidate(0, 95.0, due_hours_ago=720),
        candidate(1, 10.0, due_hours_ago=1),
    ]
    assert order(due_candidates(given, now=NOW)) == [1, 0]


def test_a_never_scheduled_concept_sorts_ahead_of_dated_ones() -> None:
    """Never having been scheduled is the most neglected a concept can be."""
    given = [
        candidate(0, 50.0, due_hours_ago=100),
        candidate(1, 50.0, due_hours_ago=None),
    ]
    assert order(due_candidates(given, now=NOW)) == [1, 0]


def test_the_order_is_stable_across_identical_inputs() -> None:
    """Without a final tie-break a learner refreshing watches the list
    reshuffle, and nothing about that looks like a bug."""
    given = [candidate(index, 50.0, due_hours_ago=2) for index in (3, 1, 2, 0)]
    first = order(due_candidates(given, now=NOW))
    shuffled = order(due_candidates(list(reversed(given)), now=NOW))

    assert first == shuffled == sorted(first)


# ------------------------------------------------------------- what is due


def test_a_concept_not_yet_due_is_left_out() -> None:
    """Including it would surface a concept attempted minutes ago, whose mastery
    is still low — spaced repetition switched off."""
    given = [candidate(0, 10.0, due_hours_ago=-24), candidate(1, 90.0, due_hours_ago=1)]
    assert order(due_candidates(given, now=NOW)) == [1]


def test_a_concept_due_exactly_now_is_included() -> None:
    given = [candidate(0, 50.0, due_hours_ago=0)]
    assert len(due_candidates(given, now=NOW)) == 1


def test_a_mastered_concept_still_comes_back_when_due() -> None:
    """Review exists to retain, not only to repair; SM-2 already gave it a long
    interval, so its turn arriving means it has earned one."""
    assert len(due_candidates([candidate(0, 100.0)], now=NOW)) == 1


def test_nothing_due_gives_an_empty_queue() -> None:
    given = [candidate(0, 10.0, due_hours_ago=-1), candidate(1, 20.0, due_hours_ago=-48)]
    queue = build_review_queue(given, minutes=10, now=NOW)

    assert queue.is_empty
    assert queue.skipped == 0


def test_an_empty_input_is_not_an_error() -> None:
    assert build_review_queue([], minutes=5, now=NOW).is_empty


# ---------------------------------------------------------------- the slicing


@pytest.mark.parametrize("minutes", REVIEW_DURATION_CHOICES)
def test_every_offered_duration_fills_its_slot(minutes: int) -> None:
    given = [candidate(index, float(index * 5), seconds=60) for index in range(11)]
    queue = build_review_queue(given, minutes=minutes, now=NOW)

    assert queue.estimated_seconds <= minutes * 60
    assert len(queue.entries) == min(minutes, 11)


def test_the_slice_keeps_the_weakest_and_drops_the_rest() -> None:
    given = [candidate(index, float(90 - index * 10), seconds=60) for index in range(8)]
    queue = build_review_queue(given, minutes=5, now=NOW)

    assert order(queue.entries) == [7, 6, 5, 4, 3]
    assert queue.skipped == 3


def test_what_did_not_fit_is_reported() -> None:
    """So a client can say "12 more waiting" rather than implying completion."""
    given = [candidate(index, 50.0, seconds=120) for index in range(10)]
    queue = build_review_queue(given, minutes=10, now=NOW)

    assert len(queue.entries) == 5
    assert queue.skipped == 5


def test_concepts_of_mixed_length_pack_in_order_not_by_best_fit() -> None:
    """Weakest first stays the rule; a shorter concept does not jump the queue
    to use up the remainder."""
    given = [
        candidate(0, 10.0, seconds=240),
        candidate(1, 20.0, seconds=240),
        candidate(2, 30.0, seconds=30),
    ]
    queue = build_review_queue(given, minutes=5, now=NOW)

    assert order(queue.entries) == [0]
    assert queue.skipped == 2


def test_a_concept_longer_than_the_whole_slot_is_still_returned() -> None:
    """Telling a learner with twenty overdue concepts that there is nothing to
    review, because each takes six minutes and they picked five, is the worse
    answer (D-017)."""
    given = [candidate(0, 10.0, seconds=600), candidate(1, 20.0, seconds=600)]
    queue = build_review_queue(given, minutes=5, now=NOW)

    assert len(queue.entries) == 1
    assert queue.over_budget is True
    assert queue.skipped == 1


def test_a_queue_inside_its_budget_is_not_flagged_over() -> None:
    given = [candidate(0, 10.0, seconds=60)]
    assert build_review_queue(given, minutes=5, now=NOW).over_budget is False


def test_an_exactly_full_slot_is_not_over_budget() -> None:
    given = [candidate(index, float(index), seconds=60) for index in range(5)]
    queue = build_review_queue(given, minutes=5, now=NOW)

    assert len(queue.entries) == 5
    assert queue.estimated_seconds == 300
    assert queue.over_budget is False


def test_the_reported_time_is_the_sum_of_what_was_kept() -> None:
    given = [candidate(0, 10.0, seconds=45), candidate(1, 20.0, seconds=75)]
    queue = build_review_queue(given, minutes=5, now=NOW)

    assert queue.estimated_seconds == 120


# ----------------------------------------------------------------- bad input


@pytest.mark.parametrize("minutes", [0, 1, 7, 30, -5])
def test_a_duration_the_interface_does_not_offer_is_refused(minutes: int) -> None:
    with pytest.raises(ValueError, match="not an offered review duration"):
        build_review_queue([], minutes=minutes, now=NOW)


def test_the_error_lists_the_offered_durations() -> None:
    with pytest.raises(ValueError, match=r"\[5, 10, 15, 25\]"):
        build_review_queue([], minutes=3, now=NOW)


def test_a_naive_now_is_refused() -> None:
    """Comparing it against a stored next_due_at either raises or silently
    compares wrong, depending on which side is naive."""
    with pytest.raises(ValueError, match="timezone-aware"):
        build_review_queue([], minutes=5, now=datetime.datetime(2026, 9, 16, 12, 0))


def test_the_offered_durations_are_the_documented_ones() -> None:
    assert REVIEW_DURATION_CHOICES == (5, 10, 15, 25)


# ------------------------------------------------- a realistic distribution


def test_a_mixed_backlog_produces_the_expected_session() -> None:
    """The shape D4 will serve: some overdue, some not, a spread of mastery."""
    given = [
        candidate(0, 85.0, due_hours_ago=2),
        candidate(1, 12.0, due_hours_ago=48),
        candidate(2, 55.0, due_hours_ago=-12),  # not due yet
        candidate(3, 12.0, due_hours_ago=3),
        candidate(4, 40.0, due_hours_ago=1),
        candidate(5, 95.0, due_hours_ago=-72),  # not due yet
    ]
    queue = build_review_queue(given, minutes=5, now=NOW)

    # Both 12.0 concepts first, the more overdue of them leading.
    assert order(queue.entries) == [1, 3, 4, 0]
    assert queue.skipped == 0
    assert queue.estimated_seconds == 240
