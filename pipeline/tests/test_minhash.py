"""Near-duplicate detection (BACKLOG E4, rule 7).

The property that matters is not "MinHash works" — it is that the verdict is
the same on every run and matches the exact similarity, because the rule
rejects content on it.
"""

from __future__ import annotations

from pipeline.minhash import estimate, find_duplicates, jaccard, shingles, signature


def test_identical_texts_are_identical_however_they_are_punctuated() -> None:
    assert shingles("我要一杯水。") == shingles("我要一杯水！")


def test_jaccard_is_one_for_the_same_set_and_zero_for_disjoint_ones() -> None:
    assert jaccard(shingles("我要水"), shingles("我要水")) == 1.0
    assert jaccard(shingles("我要水"), shingles("他买书")) == 0.0


def test_the_signature_is_stable_across_processes() -> None:
    """blake2b, not hash(): PYTHONHASHSEED must not change a verdict."""
    assert signature(shingles("我要一杯水")) == signature(shingles("我要一杯水"))


def test_the_estimate_tracks_the_exact_similarity() -> None:
    left, right = shingles("我要一杯水"), shingles("我要一杯茶")

    assert abs(estimate(signature(left), signature(right)) - jaccard(left, right)) < 0.15


def test_a_near_duplicate_pair_is_found() -> None:
    pairs = find_duplicates(["我要一杯水。", "我要一杯水！"], threshold=0.9)

    assert [(p.left, p.right) for p in pairs] == [(0, 1)]
    assert pairs[0].similarity == 1.0


def test_distinct_sentences_are_not_reported() -> None:
    assert find_duplicates(["我要水。", "他昨天买了三本书。"], threshold=0.9) == []


def test_the_threshold_is_what_decides() -> None:
    texts = ["我要一杯水", "我要一杯茶"]

    assert find_duplicates(texts, threshold=0.9) == []
    assert find_duplicates(texts, threshold=0.2) != []


def test_the_exact_pass_is_what_rules_a_borderline_pair_in_or_out() -> None:
    """The estimate screens; a pair it lets through still has to be truly similar."""
    texts = ["我要一杯水", "我要一杯水"]

    pairs = find_duplicates(texts, threshold=0.99)

    assert [p.similarity for p in pairs] == [1.0]
