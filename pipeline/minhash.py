"""Near-duplicate detection over sentences (PRD 6.2, rule 7).

MinHash, as the PRD names it, with one addition: every candidate pair MinHash
proposes is then measured exactly. A 128-permutation signature estimates
Jaccard to within roughly ±0.09, and the rule rejects at 0.9 — so on a
signature alone, whether a pair is refused would partly be noise. MinHash
narrows the field in linear time; the exact set comparison decides. At one
level's few hundred sentences the exact pass is free, and the structure is what
keeps this linear when nine levels exist.

Hashing is blake2b, not Python's ``hash``: ``hash`` is randomised per process
by PYTHONHASHSEED, which would make the same content pass one run and fail the
next. A validator whose verdict depends on the run is not a validator.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Final

#: Characters that carry no similarity signal. Dropped before shingling so that
#: a sentence ending in a full stop and the same one ending in an
#: exclamation mark are recognised as identical.
_NOISE: Final = re.compile(r"[\s\u3000-\u303f\uff00-\uffef!-/:-@\[-`{-~]")

#: Character n-grams. Two is right for Chinese: a sentence of eight characters
#: yields seven shingles, enough signal to compare, while three would leave
#: six-character sentences with only four.
SHINGLE_SIZE: Final = 2

#: Permutations in a signature. 128 is the usual default; the exact pass after
#: it means this number trades speed against recall, not against correctness.
PERMUTATIONS: Final = 128

_MAX_HASH: Final = (1 << 32) - 1


def shingles(text: str, *, size: int = SHINGLE_SIZE) -> frozenset[str]:
    """Character n-grams of ``text``, punctuation and spacing removed.

    A text shorter than one shingle becomes a single shingle of itself, so that
    two identical short strings still compare as identical rather than as two
    empty sets.
    """
    cleaned = _NOISE.sub("", text)
    if len(cleaned) <= size:
        return frozenset({cleaned}) if cleaned else frozenset()
    return frozenset(cleaned[i : i + size] for i in range(len(cleaned) - size + 1))


def jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    """Exact Jaccard similarity. Two empty sets are identical, not undefined."""
    if not left and not right:
        return 1.0
    union = len(left | right)
    return len(left & right) / union if union else 0.0


def _hash(value: str, seed: int) -> int:
    digest = hashlib.blake2b(
        value.encode("utf-8"), digest_size=4, person=seed.to_bytes(4, "little")
    ).digest()
    return int.from_bytes(digest, "big")


def signature(items: frozenset[str], *, permutations: int = PERMUTATIONS) -> tuple[int, ...]:
    """MinHash signature: the smallest hash under each permutation."""
    if not items:
        return (_MAX_HASH,) * permutations
    return tuple(min(_hash(item, seed) for item in items) for seed in range(permutations))


def estimate(left: tuple[int, ...], right: tuple[int, ...]) -> float:
    """Estimated Jaccard: the fraction of signature positions that agree."""
    if not left or len(left) != len(right):
        raise ValueError("signatures must be the same non-zero length")
    return sum(1 for a, b in zip(left, right, strict=True) if a == b) / len(left)


@dataclass(frozen=True)
class DuplicatePair:
    """Two texts too alike to both ship, with the similarity that condemned them."""

    left: int
    right: int
    similarity: float


def find_duplicates(
    texts: list[str], *, threshold: float, permutations: int = PERMUTATIONS
) -> list[DuplicatePair]:
    """Index pairs whose exact similarity exceeds ``threshold``.

    The estimate screens, the exact comparison decides — and the screen is
    deliberately generous (it keeps anything the estimate puts within a
    permutation-error margin of the threshold), because a pair dropped at the
    screening stage is never looked at again.
    """
    sets = [shingles(text) for text in texts]
    signatures = [signature(item, permutations=permutations) for item in sets]

    # One standard error of a `permutations`-sample binomial, doubled. At 128
    # permutations that is about 0.09, which is the published accuracy of a
    # signature this size.
    margin = 2 * (0.25 / permutations) ** 0.5

    found: list[DuplicatePair] = []
    for left in range(len(texts)):
        for right in range(left + 1, len(texts)):
            if estimate(signatures[left], signatures[right]) < threshold - margin:
                continue
            exact = jaccard(sets[left], sets[right])
            if exact > threshold:
                found.append(DuplicatePair(left=left, right=right, similarity=exact))
    return found
