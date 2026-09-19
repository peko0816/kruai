"""Stage [3]: the eight checks a draft has to survive (PRD 6.2, BACKLOG E4).

| rule | refuses |
|---|---|
| ``wordlist`` | a word above the level being taught |
| ``length``   | a sentence longer than the level allows |
| ``pinyin``   | pinyin that does not match the characters |
| ``khmer``    | a Khmer field that is not written in Khmer |
| ``coverage`` | a concept with too little material to teach from |
| ``hskk``     | a concept with no HSKK task type, or a turn of a type it is not graded as |
| ``duplicate``| two sentences too alike to both ship |
| ``sources``  | content whose provenance was lost (red line R7) |

Every rule is a plain function over plain data, and the runner is what walks a
draft and calls them. That shape is deliberate: a rule that can only be
exercised through a whole draft can only be tested with a whole draft, and PRD
6.2 asks for one passing and one failing case per rule.

Failures are reported, never fixed. PRD 6.1 sends the offending concepts back
to generation; a validator that quietly repaired its input would make the next
run's output depend on this one's.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pypinyin import Style, pinyin

from app.core.config import Settings, get_settings
from pipeline.draft import ConceptDraft, Draft
from pipeline.minhash import find_duplicates
from pipeline.wordlist import MissingWordlistError, Wordlist, load_wordlist, uncovered

#: Khmer, U+1780-U+17FF (CODING_STANDARDS section 11).
_KHMER: Final = re.compile(r"[\u1780-\u17ff]")

#: Han characters. What the length rule counts, and what a Khmer field must not
#: be written in.
_HAN: Final = re.compile(r"[\u4e00-\u9fff]")

#: A translation nobody has written yet. Allowed in a seed (D-079), never in a
#: draft: this is the stage that would let one through to a learner.
_PLACEHOLDER: Final = re.compile(r"\[\[km:[^\]]*\]\]")

#: Everything that is not a syllable when comparing pinyin: tone-neutral
#: separators the standard uses (dì//diǎn), punctuation, and spacing.
_PINYIN_NOISE: Final = re.compile(r"[\s/·,.!?;:()\u3000-\u303f\uff00-\uffef-]+")


@dataclass(frozen=True)
class Violation:
    """One rule, one place, one reason."""

    rule: str
    where: str
    message: str

    def __str__(self) -> str:
        return f"[{self.rule}] {self.where}: {self.message}"


# ---------------------------------------------------------------- the rules


def check_sources(
    sources: Sequence[Mapping[str, object]], licence: str, *, where: str
) -> list[Violation]:
    """Rule 8, red line R7: content without provenance must not ship (PRD 5.5)."""
    problems: list[Violation] = []
    if not sources:
        problems.append(Violation("sources", where, "no sources; PRD 5.5 requires at least one"))
    if not licence.strip():
        problems.append(Violation("sources", where, "licence is blank"))
    for index, source in enumerate(sources):
        identifier = str(source.get("id", "")).strip()
        if not identifier:
            problems.append(Violation("sources", f"{where}.sources[{index}]", "source has no id"))
    return problems


def check_length(sentence: str, *, maximum: int, where: str) -> list[Violation]:
    """Rule 2. Counts Han characters; punctuation is not what makes a sentence long."""
    length = len(_HAN.findall(sentence))
    if length > maximum:
        return [
            Violation(
                "length",
                where,
                f"{length} characters, limit {maximum} for this level: {sentence!r}",
            )
        ]
    return []


def check_wordlist(sentence: str, wordlist: Wordlist, *, where: str) -> list[Violation]:
    """Rule 1. Anything the level's vocabulary cannot account for."""
    missing = uncovered(sentence, wordlist)
    if missing:
        return [
            Violation(
                "wordlist",
                where,
                f"outside the {wordlist.level} vocabulary: {missing} in {sentence!r}",
            )
        ]
    return []


def check_pinyin(characters: str, written: str, *, where: str) -> list[Violation]:
    """Rule 3. Regenerate with pypinyin and compare, syllable by syllable.

    Heteronyms are accepted rather than fought: 了 is le and liǎo, 不 changes
    tone before a fourth tone, and a reading that is right in context would
    otherwise be reported as an error on every sentence that used it. So each
    position is compared against *every* reading pypinyin knows for that
    character, and only a syllable that matches none of them is a violation.
    That still catches what the rule is for — a model writing pinyin that does
    not belong to the characters it wrote.
    """
    expected = pinyin(characters, style=Style.TONE, heteronym=True, errors="ignore")
    actual = [syllable for syllable in _PINYIN_NOISE.split(written.strip().lower()) if syllable]

    if len(expected) != len(actual):
        return [
            Violation(
                "pinyin",
                where,
                f"{len(actual)} syllables for {len(expected)} characters: "
                f"{written!r} against {characters!r}",
            )
        ]

    for index, (readings, syllable) in enumerate(zip(expected, actual, strict=True)):
        if syllable not in {reading.lower() for reading in readings}:
            return [
                Violation(
                    "pinyin",
                    where,
                    f"syllable {index + 1} is {syllable!r}; {characters[index]!r} reads "
                    f"{sorted(readings)}",
                )
            ]
    return []


def check_khmer(text: str, *, where: str) -> list[Violation]:
    """Rule 4. A Khmer field has to be Khmer: not English, not Chinese, not a placeholder."""
    if not text.strip():
        return [Violation("khmer", where, "empty")]
    if _PLACEHOLDER.search(text):
        return [
            Violation(
                "khmer",
                where,
                "still a placeholder; a draft is where these get written, not carried through",
            )
        ]
    if not _KHMER.search(text):
        return [
            Violation(
                "khmer",
                where,
                f"no Khmer script (U+1780-U+17FF): {text!r}",
            )
        ]
    return []


def check_coverage(
    concept: ConceptDraft,
    *,
    min_sentences: int,
    min_substitutions: int,
    min_dialogues: int,
    where: str,
) -> list[Violation]:
    """Rule 5. Too little material is a concept that cannot be taught."""
    problems: list[Violation] = []
    for name, actual, wanted in (
        ("target sentences", len(concept.target_sentences), min_sentences),
        ("substitutions", len(concept.substitutions), min_substitutions),
        ("dialogue tasks", len(concept.dialogues), min_dialogues),
    ):
        if actual < wanted:
            problems.append(Violation("coverage", where, f"{actual} {name}, needs {wanted}"))
    return problems


def check_hskk(concept: ConceptDraft, *, where: str) -> list[Violation]:
    """Rule 6. No task type means nothing outside our judgement grades the turn."""
    if not concept.hskk_task_types:
        return [Violation("hskk", where, "no HSKK task type (PRD 6.2)")]

    problems: list[Violation] = []
    allowed = set(concept.hskk_task_types)
    for index, dialogue in enumerate(concept.dialogues):
        if dialogue.hskk_task_type not in allowed:
            problems.append(
                Violation(
                    "hskk",
                    f"{where}.dialogues[{index}]",
                    f"task type {dialogue.hskk_task_type!r} is not one this concept is "
                    f"graded as ({sorted(allowed)})",
                )
            )
    return problems


def check_duplicates(labelled: Sequence[tuple[str, str]], *, threshold: float) -> list[Violation]:
    """Rule 7. Sentence-level near-duplicates.

    Takes (where, sentence) pairs so the report can name both sides of a pair:
    knowing two sentences collide is useless without knowing which two.

    The caller decides what is compared with what, and it compares each kind of
    sentence with its own kind only (D-088): a dialogue whose model answer is
    one of the drill sentences is ordinary teaching — the learner has just
    practised that sentence — while two drill sentences that differ by a
    punctuation mark are one wasted slot.
    """
    duplicates = find_duplicates([text for _, text in labelled], threshold=threshold)
    return [
        Violation(
            "duplicate",
            labelled[pair.left][0],
            f"{pair.similarity:.2f} similar to {labelled[pair.right][0]}: "
            f"{labelled[pair.left][1]!r} / {labelled[pair.right][1]!r}",
        )
        for pair in duplicates
    ]


# --------------------------------------------------------------- the runner


def max_characters(level: str, settings: Settings) -> int:
    """The length limit for a level (PRD 6.2).

    Raises:
        ValueError: no limit is configured for this level. Refused rather than
            defaulted: a level nobody set a limit for is a level whose sentences
            nobody decided the shape of, and silently allowing any length would
            make the rule vanish exactly where it was never considered.
    """
    limits = {
        "HSK1": settings.pipeline_max_chars_hsk1,
        "HSK2": settings.pipeline_max_chars_hsk2,
        "HSK3": settings.pipeline_max_chars_hsk3,
    }
    try:
        return limits[level.upper()]
    except KeyError:
        raise ValueError(
            f"no PIPELINE_MAX_CHARS_* configured for level {level!r}; "
            f"configured levels are {sorted(limits)}"
        ) from None


def validate(draft: Draft, *, settings: Settings, wordlist: Wordlist) -> list[Violation]:
    """Run every rule over a draft and return everything wrong with it."""
    problems: list[Violation] = check_sources(draft.sources, draft.licence, where="draft")
    maximum = max_characters(draft.level, settings)
    #: Compared within a kind, never across kinds. See check_duplicates.
    by_kind: dict[str, list[tuple[str, str]]] = {
        "target_sentences": [],
        "dialogue_prompts": [],
        "sample_answers": [],
    }

    for index, concept in enumerate(draft.concepts):
        where = f"concepts[{index}]({concept.slug})"

        problems += check_khmer(concept.km_explanation, where=f"{where}.km_explanation")
        problems += check_coverage(
            concept,
            min_sentences=settings.pipeline_min_sentences_per_concept,
            min_substitutions=settings.pipeline_min_substitutions_per_concept,
            min_dialogues=settings.pipeline_min_dialogues_per_concept,
            where=where,
        )
        problems += check_hskk(concept, where=where)

        for position, sentence in enumerate(concept.target_sentences):
            at = f"{where}.target_sentences[{position}]"
            problems += check_length(sentence.zh, maximum=maximum, where=at)
            problems += check_wordlist(sentence.zh, wordlist, where=at)
            problems += check_pinyin(sentence.zh, sentence.pinyin, where=at)
            problems += check_khmer(sentence.km_gloss, where=f"{at}.km_gloss")
            by_kind["target_sentences"].append((at, sentence.zh))

        for position, substitution in enumerate(concept.substitutions):
            at = f"{where}.substitutions[{position}]"
            problems += check_wordlist(substitution.zh, wordlist, where=at)
            problems += check_pinyin(substitution.zh, substitution.pinyin, where=at)
            problems += check_khmer(substitution.km_gloss, where=f"{at}.km_gloss")

        for position, dialogue in enumerate(concept.dialogues):
            at = f"{where}.dialogues[{position}]"
            problems += check_length(dialogue.prompt_zh, maximum=maximum, where=f"{at}.prompt_zh")
            problems += check_wordlist(dialogue.prompt_zh, wordlist, where=f"{at}.prompt_zh")
            problems += check_wordlist(
                dialogue.sample_answer_zh, wordlist, where=f"{at}.sample_answer_zh"
            )
            problems += check_khmer(dialogue.prompt_km, where=f"{at}.prompt_km")
            by_kind["dialogue_prompts"].append((f"{at}.prompt_zh", dialogue.prompt_zh))
            by_kind["sample_answers"].append((f"{at}.sample_answer_zh", dialogue.sample_answer_zh))

    for labelled in by_kind.values():
        problems += check_duplicates(labelled, threshold=settings.pipeline_dedup_threshold)
    return problems


# ------------------------------------------------------------------------ CLI


def report(problems: list[Violation], *, draft: Draft) -> int:
    """Print what is wrong, grouped by rule. Non-zero means do not build this."""
    by_rule: dict[str, list[Violation]] = {}
    for problem in problems:
        by_rule.setdefault(problem.rule, []).append(problem)

    for rule in sorted(by_rule):
        for problem in by_rule[rule]:
            print(f"error: {problem}", file=sys.stderr)

    print(f"{len(draft.concepts)} concept(s) checked against 8 rules (PRD 6.2)")
    if by_rule:
        counts = ", ".join(f"{rule}: {len(items)}" for rule, items in sorted(by_rule.items()))
        print(f"{len(problems)} violation(s) — {counts}")
        return 1
    print("no violations")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("draft", type=Path, help="the draft JSON written by generate.py")
    arguments = parser.parse_args(argv)

    settings = get_settings()
    try:
        draft = Draft.model_validate_json(arguments.draft.read_text(encoding="utf-8"))
    except OSError as error:
        print(f"error: cannot read {arguments.draft}: {error.strerror or error}", file=sys.stderr)
        return 1

    try:
        wordlist = load_wordlist(draft.language, draft.level)
    except (MissingWordlistError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    return report(validate(draft, settings=settings, wordlist=wordlist), draft=draft)


if __name__ == "__main__":
    raise SystemExit(main())
