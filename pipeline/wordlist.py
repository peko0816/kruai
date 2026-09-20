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

import itertools
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

_ROOT: Final = Path(__file__).resolve().parents[1]

WORDLIST_DIR: Final = _ROOT / "pipeline" / "wordlists"

#: Characters a sentence may contain that are not words: punctuation, digits,
#: latin letters and spaces. Everything else has to be covered by the list.
_SKIPPABLE: Final = re.compile(
    "[\\s0-9A-Za-z"
    "\\u2000-\\u206f"  # general punctuation: the curly quotes around 引语
    "\\u3000-\\u303f"  # CJK punctuation: 。，、；：！？「」
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


# --------------------------------------------------------- the source table
#
# A word list is derived from a transcription of the standard's table, kept
# beside it as `<level>.source.tsv`. Deriving rather than typing the list twice
# is what lets a test assert the two agree: a correction goes into the
# transcription, and anything that did not come from the table cannot appear in
# the list.


@dataclass(frozen=True)
class SourceEntry:
    """One numbered row of the table, as printed."""

    number: int
    #: The word column, with the notation the table uses: 爸爸|爸, 白（形）,
    #: 好玩ㄦ, 有（一）些.
    word: str
    #: The pinyin column, with its own notation: bāng//máng, chū/·lái.
    reading: str


#: Part-of-speech and example annotations, always at the end of the word.
_TRAILING_NOTE: Final = re.compile(r"（[^）]*）$")

#: An optional element *inside* a word — 有（一）些 — where both forms are words
#: in their own right. Position is what distinguishes it from an annotation.
_INNER_OPTION: Final = re.compile(r"^(.*?)（([^）]*)）(.+)$")

#: The table writes erhua as a small 儿. Both spellings occur in real text.
_ERHUA: Final = "ㄦ"


def read_source(path: Path) -> list[SourceEntry]:
    """Read a transcription table: number, word, reading, tab separated."""
    entries: list[SourceEntry] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        number, word, reading = line.split("\t")
        entries.append(SourceEntry(number=int(number), word=word, reading=reading))
    return entries


def expand_entry(word: str) -> tuple[str, ...]:
    """Every form of one table row, in the order they should be written out.

    The table's notation carries three different things and they expand
    differently, which is why this is a function with tests rather than a
    regular expression at a call site:

        爸爸|爸     -> both, they are alternative words
        白（形）    -> 白, the bracket is a part of speech
        有（一）些  -> 有些 and 有一些, the bracket is part of the word
        好玩ㄦ      -> 好玩 and 好玩儿, both spellings occur
    """
    forms: list[str] = []
    for variant in word.split("|"):
        variant = _TRAILING_NOTE.sub("", variant).strip()
        if not variant:
            continue

        candidates = [variant]
        inner = _INNER_OPTION.match(variant)
        if inner:
            before, optional, after = inner.groups()
            candidates = [f"{before}{after}", f"{before}{optional}{after}"]

        for candidate in candidates:
            if _ERHUA in candidate:
                base = candidate.replace(_ERHUA, "")
                forms.extend([base, f"{base}儿"])
            else:
                forms.append(candidate)

    # Order-preserving dedup: 零|〇 and a repeated form should appear once.
    seen: dict[str, None] = {}
    for form in forms:
        seen.setdefault(form, None)
    return tuple(seen)


def expand_source(entries: Sequence[SourceEntry]) -> tuple[str, ...]:
    """Every word form in a table, table order, each appearing once."""
    forms: dict[str, None] = {}
    for entry in entries:
        for form in expand_entry(entry.word):
            forms.setdefault(form, None)
    return tuple(forms)


def pinyin_agrees(word: str, reading: str) -> bool:
    """Do a row's characters and its own pinyin column describe the same word?

    The two columns were read separately from a scan, so a misread character
    is very unlikely to still match the pinyin beside it. This is that check,
    and it is here rather than in a one-off script because the next level's
    transcription will want it too.

    Two passes, and the split is the point. The syllables must first line up
    with the characters ignoring tone, which is what a misread character
    breaks. Then tone is judged syllable by syllable, allowing only the two
    differences the table really has: neutral tone written without a mark
    (爸爸 bàba against the dictionary's bàbà), and the sandhi tones of 不 and
    一 (bú dà, yìxiē).

    Accepting any tone difference would be simpler and would leave this blind
    to tone altogether — which is what a mutation demonstrated before it was
    written this way.
    """
    if "|" in word:
        # 爸爸|爸 with bàba|bà: two words on one row, each with its own reading.
        readings = reading.split("|")
        variants = word.split("|")
        if len(readings) != len(variants):
            return False
        return all(
            pinyin_agrees(variant, variant_reading)
            for variant, variant_reading in zip(variants, readings, strict=True)
        )

    target = _plain(reading)
    if " / " in reading:  # 谁 shéi / shuí: alternative readings, not a sequence
        target = _plain(reading.split(" / ")[0])
    stripped = word.replace(_ERHUA, "")
    stripped = _TRAILING_NOTE.sub("", stripped).replace("（", "").replace("）", "")

    for candidate in (target, target.removesuffix("r")):
        syllables = _split_syllables(stripped, candidate)
        if syllables is not None and _tones_agree(stripped, syllables):
            return True
    return False


def _plain(reading: str) -> str:
    """The pinyin column with the table's notation removed."""
    for noise in ("//", "/", "·", "(", ")", "（", "）", "'", "’", " "):
        reading = reading.replace(noise, "")
    return reading.lower()


def _detone(syllables: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFD", syllables)
        if unicodedata.category(character) != "Mn"
    )


#: Characters whose tone changes with what follows, so the table's mark
#: legitimately differs from the citation tone. 不 and 一 are the two the
#: standard writes that way (bú dà, yìxiē).
_SANDHI: Final = frozenset("不一")


def _readings(word: str) -> list[list[str]]:
    # errors="ignore" drops anything that is not a Han character, which is
    # what we want: a row's pinyin describes its characters, and a stray
    # bracket has no reading to compare.
    from pypinyin import Style, pinyin

    return pinyin(word, style=Style.TONE, heteronym=True, errors="ignore")


def _split_syllables(word: str, target: str) -> list[str] | None:
    """Cut ``target`` into one syllable per character, ignoring tone.

    Returns the pieces of the table's own pinyin so the caller can judge their
    tones separately; None when no cut lines up, which is what a misread
    character looks like.
    """
    readings = _readings(word)

    def walk(index: int, rest: str, taken: list[str]) -> list[str] | None:
        if index == len(readings):
            return taken if rest == "" else None
        for reading in readings[index]:
            bare = _detone(_plain(reading))
            if not bare or not _detone(rest).startswith(bare):
                continue
            piece = _take(rest, len(bare))
            found = walk(index + 1, rest[len(piece) :], [*taken, piece])
            if found is not None:
                return found
        return None

    return walk(0, target, [])


def _take(text: str, bare_length: int) -> str:
    """The prefix of ``text`` that is ``bare_length`` long once tones are stripped."""
    for size in range(bare_length, len(text) + 1):
        if len(_detone(text[:size])) == bare_length:
            return text[:size]
    return text


def _tones_agree(word: str, syllables: Sequence[str]) -> bool:
    """Does every syllable carry a tone that character can have?

    Two differences are allowed and no others: a syllable written with no tone
    mark at all is neutral tone, and 不 and 一 are written with their sandhi
    tone. Allowing any difference would make the whole check blind to tone.
    """
    readings = _readings(word)
    characters = [character for character in word if _is_han(character)]

    for index, syllable in enumerate(syllables):
        if index >= len(readings):
            return False
        if syllable in {_plain(reading) for reading in readings[index]}:
            continue
        if syllable == _detone(syllable):
            continue
        if index < len(characters) and characters[index] in _SANDHI:
            continue
        return False
    return True


def _is_han(character: str) -> bool:
    return "一" <= character <= "鿿" or character == "〇"


#: Combining tone marks, in the order a Chinese dictionary sorts them. Neutral
#: tone (no mark) sorts last, which is why it is 5 rather than 0.
_TONE_MARKS: Final[dict[str, int]] = {
    "̄": 1,  # macron
    "́": 2,  # acute
    "̌": 3,  # caron
    "̀": 4,  # grave
}


def sort_key(entry: SourceEntry) -> tuple[str, tuple[tuple[str, int], ...]] | None:
    """Where a row belongs in the table's own ordering, or None if unsplittable.

    The table is a dictionary: syllable by syllable, letters first and then
    tone, with neutral tone last. Checking a transcription against that order
    is a third thing the pinyin column has to satisfy, independent of both the
    numbering and the agreement with the characters — a misread syllable
    usually lands in the wrong place.

    Returns the head character alongside the key, because the table groups
    homophones by character: every 地 word, then every 弟 word, then 第. Those
    are the only points where the order legitimately steps backwards.
    """
    reading = entry.reading.split("|")[0].split(" / ")[0]
    head = _TRAILING_NOTE.sub("", entry.word.split("|")[0])
    head = head.replace(_ERHUA, "").replace("（", "").replace("）", "")
    target = _plain(reading)

    syllables = _split_syllables(head, target)
    erhua = _ERHUA in entry.word
    if syllables is None:
        syllables = _split_syllables(head, target.removesuffix("r"))
        erhua = erhua or syllables is not None
    if syllables is None:
        return None

    key = [(_detone(syllable).lower(), _tone_of(syllable)) for syllable in syllables]
    if erhua:
        key.append(("r", 5))
    return head[:1], tuple(key)


def _tone_of(syllable: str) -> int:
    for character in unicodedata.normalize("NFD", syllable):
        if character in _TONE_MARKS:
            return _TONE_MARKS[character]
    return 5


def order_breaks(entries: Sequence[SourceEntry]) -> list[tuple[SourceEntry, SourceEntry]]:
    """Consecutive rows the table's own ordering cannot explain.

    A step backwards is expected where the table moves from one homophone
    character to the next — 男生 to 南, 坐下 to 做 — and nowhere else.
    """
    found: list[tuple[SourceEntry, SourceEntry]] = []
    for previous, current in itertools.pairwise(entries):
        before, after = sort_key(previous), sort_key(current)
        if before is None or after is None:
            found.append((previous, current))
            continue
        (head_before, key_before), (head_after, key_after) = before, after
        if key_after >= key_before:
            continue
        if key_before[:1] == key_after[:1] and head_before != head_after:
            continue
        found.append((previous, current))
    return found
