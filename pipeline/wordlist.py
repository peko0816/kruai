"""The vocabulary a level is allowed to use, and how a sentence is checked against it.

BACKLOG E4, rule 1 of PRD 6.2: every word in a generated sentence must be in
the target level's word list or below. The list itself is public — the word
tables of GF 0025-2021 are inside the R7 whitelist (PRD 5.5) — and lives as
data under ``pipeline/wordlists/``, not in code.

**Segmentation is done with the word list itself.** Chinese has no spaces, so
"is every word in the list?" normally needs a segmenter, and a segmenter brings
its own vocabulary that disagrees with ours at exactly the interesting places.
Longest-match segmentation over the permitted vocabulary answers the question
directly instead: walk the sentence taking the longest run that is a listed
word, and whatever cannot be covered that way is, by definition, out of
vocabulary. No dependency, and no second opinion about what a word is.

A missing list is an error, never a skip. A rule that silently passes because
its data is absent is worse than no rule: the build goes green and the level
boundary is unenforced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

_ROOT: Final = Path(__file__).resolve().parents[1]

WORDLIST_DIR: Final = _ROOT / "pipeline" / "wordlists"

#: Characters a sentence may contain that are not words: punctuation, digits,
#: latin letters and spaces. Everything else has to be covered by the list.
_SKIPPABLE: Final = re.compile(
    "[\\s0-9A-Za-z"
    "\\u3000-\\u303f"  # CJK punctuation
    "\\uff00-\\uffef"  # fullwidth forms
    "!-/:-@\\[-`{-~"  # ASCII punctuation
    "]"
)


class MissingWordlistError(FileNotFoundError):
    """No word list for a level. Refused rather than skipped."""


@dataclass(frozen=True)
class Wordlist:
    """One level's permitted vocabulary, cumulative of the levels below it."""

    language: str
    level: str
    words: frozenset[str]
    #: Where it came from, for R7. Lines beginning ``# source:`` in the file.
    sources: tuple[str, ...] = ()

    @property
    def longest_word(self) -> int:
        return max((len(word) for word in self.words), default=0)


def wordlist_path(language: str, level: str, *, directory: Path | None = None) -> Path:
    # Resolved on the call, not bound as a default: a default argument freezes
    # WORDLIST_DIR at import time, which makes the location untestable and
    # unoverridable for exactly the reason it would need to be.
    return (directory or WORDLIST_DIR) / language / f"{level.lower()}.txt"


def load_wordlist(language: str, level: str, *, directory: Path | None = None) -> Wordlist:
    """Read one level's word list.

    Format: one word per line, ``#`` comments, blank lines ignored. A line
    ``# source: ...`` records provenance and is kept.

    Raises:
        MissingWordlistError: no such list. Callers must not treat this as
            "nothing to check" — see the module docstring.
        ValueError: the file exists but holds no words.
    """
    path = wordlist_path(language, level, directory=directory)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise MissingWordlistError(
            f"no word list at {path}: rule 'wordlist' (PRD 6.2) cannot run without "
            f"one, and skipping it would leave the level boundary unchecked. "
            f"Original error: {error.strerror or error}"
        ) from None

    words: set[str] = set()
    sources: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# source:"):
            sources.append(stripped.removeprefix("# source:").strip())
        if not stripped or stripped.startswith("#"):
            continue
        words.add(stripped)

    if not words:
        raise ValueError(f"{path} holds no words")
    return Wordlist(language=language, level=level, words=frozenset(words), sources=tuple(sources))


def uncovered(sentence: str, wordlist: Wordlist) -> list[str]:
    """The parts of ``sentence`` the word list cannot account for.

    Longest match wins, scanning left to right. Returns the maximal runs that
    could not be covered, in order, so an error message can quote them: an
    empty list means the sentence is inside the level.

    Greedy rather than exhaustive: a segmentation that only succeeds by
    preferring a shorter word early on is rare in practice, and the cost of
    getting it wrong here is a false positive that a human reads — not silent
    acceptance of an out-of-level word.
    """
    longest = wordlist.longest_word or 1
    leftovers: list[str] = []
    current: list[str] = []
    index = 0

    while index < len(sentence):
        character = sentence[index]
        if _SKIPPABLE.match(character):
            if current:
                leftovers.append("".join(current))
                current = []
            index += 1
            continue

        matched = 0
        for size in range(min(longest, len(sentence) - index), 0, -1):
            if sentence[index : index + size] in wordlist.words:
                matched = size
                break

        if matched:
            if current:
                leftovers.append("".join(current))
                current = []
            index += matched
        else:
            current.append(character)
            index += 1

    if current:
        leftovers.append("".join(current))
    return leftovers
