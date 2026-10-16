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
import csv
import itertools
import re
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pypinyin import Style, pinyin

from app.core.config import Settings, get_settings
from pipeline.draft import ConceptDraft, Draft
from pipeline.minhash import find_duplicates
from pipeline.wordlist import (
    MissingWordlistError,
    Wordlist,
    detone,
    load_wordlist,
    split_syllables,
    syllable_agrees,
    uncovered,
)

#: Khmer, U+1780-U+17FF (CODING_STANDARDS section 11).
_KHMER: Final = re.compile(r"[\u1780-\u17ff]")

#: Han characters. What the length rule counts, and what a Khmer field must not
#: be written in.
_HAN: Final = re.compile(r"[\u4e00-\u9fff]")

#: Same as the other two exports: Excel reads a UTF-8 file without a byte-order
#: mark as the local code page, which turns every Chinese column into mojibake
#: (D-093).
ENCODING: Final = "utf-8-sig"

#: A translation nobody has written yet. Allowed in a seed (D-079), never in a
#: draft: this is the stage that would let one through to a learner.
_PLACEHOLDER: Final = re.compile(r"\[\[km:[^\]]*\]\]")

#: Everything that is not a syllable when comparing pinyin: tone-neutral
#: separators the standard uses (dì//diǎn), the syllable-dividing apostrophe
#: (xī'ān), punctuation, and spacing.
_PINYIN_NOISE: Final = re.compile(r"[\s/·'\u2019,.!?;:()\u3000-\u303f\uff00-\uffef-]+")


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

    Three conventions all have to be accepted, because all three are how
    pinyin is actually written and a model writes it correctly:

    * **Words are joined.** 书包 is ``shūbāo``, not ``shū bāo`` (GB/T 16159),
      so a space is a word boundary and counting spaces is not counting
      syllables. The syllables are found by cutting against the characters'
      own readings instead.
    * **Neutral tone carries no mark, and 不 and 一 carry their sandhi tone.**
      东西 is ``dōngxi``; 一下 is ``yíxià``. That judgement is
      ``wordlist.syllable_agrees``, shared with the transcription checker
      rather than written twice.
    * **Erhua is an r on the syllable before it.** 哪儿 is ``nǎr``: 儿 has no
      syllable of its own.

    Heteronyms are accepted rather than fought: each position is compared
    against *every* reading pypinyin knows for that character. That still
    catches what the rule is for — pinyin that does not belong to the
    characters it was written for.
    """
    han = "".join(_HAN.findall(characters))
    joined = _PINYIN_NOISE.sub("", written.strip().lower())

    for candidate, text in _readings_candidates(han, joined):
        syllables = split_syllables(candidate, text)
        if syllables is None:
            continue
        expected = pinyin(candidate, style=Style.TONE, heteronym=True, errors="ignore")
        for index, (readings, syllable) in enumerate(zip(expected, syllables, strict=True)):
            if not syllable_agrees(syllable, readings, character=candidate[index]):
                return [
                    Violation(
                        "pinyin",
                        where,
                        f"syllable {index + 1} is {syllable!r}; {candidate[index]!r} "
                        f"reads {sorted(readings)}",
                    )
                ]
        return []

    expected = pinyin(han, style=Style.TONE, heteronym=True, errors="ignore")
    stuck = _stuck_at(han, joined, expected)
    if stuck is not None:
        index, rest = stuck
        return [
            Violation(
                "pinyin",
                where,
                f"syllable {index + 1} is {rest!r}; {han[index]!r} reads {sorted(expected[index])}",
            )
        ]
    return [
        Violation(
            "pinyin",
            where,
            f"does not read as {len(expected)} syllable(s): {written!r} against {characters!r}",
        )
    ]


#: A 儿 that follows another character, which is the erhua one. A leading 儿
#: (儿子 érzi) is an ordinary syllable.
_ERHUA_SUFFIX: Final = re.compile(r"(?<=.)儿")


def _readings_candidates(han: str, joined: str) -> Iterator[tuple[str, str]]:
    """The spellings this text could legitimately be, most literal first.

    Erhua is the only one that changes how many syllables there are, and it is
    resolved by trying: if dropping a 儿 and one ``r`` makes the whole string
    cut cleanly, that was the ``r`` the 儿 was written as. Guessing which ``r``
    by position would have to tell erhua from an r-initial syllable (日 rì),
    which the spelling does not say.
    """
    yield han, joined

    count = len(_ERHUA_SUFFIX.findall(han))
    if not count or count > 2:  # two is already more erhua than a beginner sentence has
        return
    reduced = _ERHUA_SUFFIX.sub("", han)
    positions = [index for index, letter in enumerate(joined) if letter == "r"]
    for combination in itertools.combinations(positions, count):
        yield (
            reduced,
            "".join(letter for index, letter in enumerate(joined) if index not in combination),
        )


def _stuck_at(han: str, joined: str, expected: Sequence[Sequence[str]]) -> tuple[int, str] | None:
    """Where a left-to-right read of ``joined`` stops matching ``han``.

    Returns the character's index and what was left of the pinyin there, or
    None when the whole string reads but the lengths still disagree — the
    pinyin ran out early, or ran on past the end.
    """
    rest = joined
    for index, readings in enumerate(expected):
        for reading in readings:
            bare = detone(reading.lower())
            if bare and detone(rest).startswith(bare):
                rest = rest[len(_prefix_of(rest, len(bare))) :]
                break
        else:
            return index, rest
    return None


def _prefix_of(text: str, bare_length: int) -> str:
    """The prefix of ``text`` that is ``bare_length`` long once tones are gone."""
    for size in range(bare_length, len(text) + 1):
        if len(detone(text[:size])) == bare_length:
            return text[:size]
    return text


def check_khmer(text: str, *, where: str, is_gloss: bool = False) -> list[Violation]:
    """Rule 4. A Khmer field has to be Khmer: not English, not Chinese, not a placeholder.

    ``is_gloss`` tightens it to "and no Chinese either". Only two of the four
    Khmer fields are glosses, and the distinction is the whole point:

    * A **gloss** says what a Chinese line means, for somebody who cannot read
      Chinese. A character left in it is untranslated, not quoted — and the
      first real level shipped eighteen of them, ``ប្រាក់ប្រាំ元។`` among
      others, where 元 is the one word in the phrase the reader needed.
    * An **explanation** quotes the pattern it is explaining, and a dialogue
      prompt tells a learner which Chinese sentence to say. Both have to carry
      Chinese; D-079 argued that banning it there rejects the clearest ones.

    Rule 4 as it stood passed all eighteen: they do contain Khmer.
    """
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
    if is_gloss:
        left = sorted(set(_HAN.findall(text)))
        if left:
            return [
                Violation(
                    "khmer",
                    where,
                    f"leaves {left} untranslated; a gloss is read by somebody who "
                    f"cannot read Chinese: {text!r}",
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
            problems += check_khmer(sentence.km_gloss, where=f"{at}.km_gloss", is_gloss=True)
            by_kind["target_sentences"].append((at, sentence.zh))

        for position, substitution in enumerate(concept.substitutions):
            at = f"{where}.substitutions[{position}]"
            problems += check_wordlist(substitution.zh, wordlist, where=at)
            problems += check_pinyin(substitution.zh, substitution.pinyin, where=at)
            problems += check_khmer(substitution.km_gloss, where=f"{at}.km_gloss", is_gloss=True)

        for position, dialogue in enumerate(concept.dialogues):
            at = f"{where}.dialogues[{position}]"
            # The limit is about the sentence a beginner has to produce, which
            # is the sample answer — and it was the one field nobody measured.
            # A prompt is heard, not produced, and carries instruction wording
            # on top of the sentence, so it gets that wording's budget and no
            # more (PIPELINE_MAX_CHARS_PROMPT_EXTRA, D-109).
            problems += check_length(
                dialogue.prompt_zh,
                maximum=maximum + settings.pipeline_max_chars_prompt_extra,
                where=f"{at}.prompt_zh",
            )
            problems += check_length(
                dialogue.sample_answer_zh, maximum=maximum, where=f"{at}.sample_answer_zh"
            )
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


# ---------------------------------------------------------------------------
# Handing the violations to a person
#
# report() prints for whoever ran the command. A level's worth of violations is
# a different job: somebody works through them one at a time, over days, and
# needs the offending text beside the complaint rather than a path to look up.
# Note who this is for — these are Chinese-side problems (vocabulary above the
# level, duplicates, length), not the Khmer judgement review_export.py samples.

FIX_COLUMNS: Final[tuple[str, ...]] = (
    "id",
    "concept",
    "standard_ref",
    "pattern",
    "rule",
    "field",
    "chinese",
    "pinyin",
    "problem",
    "fix",
)

#: ``concepts[7](slug).target_sentences[3]`` and ``....dialogues[1].prompt_zh``.
_WHERE: Final = re.compile(
    r"concepts\[(?P<concept>\d+)\]\([^)]*\)"
    r"(?:\.(?P<kind>\w+)(?:\[(?P<item>\d+)\])?(?:\.(?P<field>\w+))?)?"
)


def _text_at(draft: Draft, where: str) -> tuple[str, str]:
    """The Chinese and pinyin the violation is about, or blanks.

    Resolved from the draft rather than scraped out of the message: a message
    is written for a reader, and a spreadsheet column is not.
    """
    found = _WHERE.match(where)
    if found is None:
        return "", ""
    concept = draft.concepts[int(found["concept"])]
    kind, item, field = found["kind"], found["item"], found["field"]
    if kind == "km_explanation":
        return concept.km_explanation, ""
    if item is None:
        return "", ""
    index = int(item)
    if kind == "target_sentences":
        sentence = concept.target_sentences[index]
        return (sentence.km_gloss, "") if field == "km_gloss" else (sentence.zh, sentence.pinyin)
    if kind == "substitutions":
        substitution = concept.substitutions[index]
        if field == "km_gloss":
            return substitution.km_gloss, ""
        return substitution.zh, substitution.pinyin
    if kind == "dialogues":
        dialogue = concept.dialogues[index]
        return getattr(dialogue, field or "prompt_zh", ""), ""
    return "", ""


def fix_rows(problems: Sequence[Violation], *, draft: Draft) -> list[dict[str, str]]:
    """One row per violation, ordered the way somebody would work through them:
    by concept, so a person stays inside one grammar point at a time."""
    by_slug = {concept.slug: concept for concept in draft.concepts}
    rows: list[dict[str, str]] = []
    for problem in problems:
        found = _WHERE.match(problem.where)
        slug = ""
        if found is not None:
            slug = draft.concepts[int(found["concept"])].slug
        concept = by_slug.get(slug)
        chinese, reading = _text_at(draft, problem.where)
        rows.append(
            {
                "id": problem.where,
                "concept": slug,
                "standard_ref": (concept.standard_ref or "") if concept else "",
                "pattern": concept.pattern if concept else "",
                "rule": problem.rule,
                "field": problem.where.partition(")")[2].lstrip("."),
                "chinese": chinese,
                "pinyin": reading,
                "problem": problem.message,
                "fix": "",
            }
        )
    rows.sort(key=lambda row: (row["standard_ref"], row["concept"], row["id"]))
    return rows


def write_fix_csv(rows: Sequence[dict[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding=ENCODING, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIX_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("draft", type=Path, help="the draft JSON written by generate.py")
    parser.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="also write the violations as a sheet somebody can work through",
    )
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

    problems = validate(draft, settings=settings, wordlist=wordlist)
    if arguments.csv is not None:
        write_fix_csv(fix_rows(problems, draft=draft), arguments.csv)
        print(f"{arguments.csv}: {len(problems)} violation(s) to work through")
    return report(problems, draft=draft)


if __name__ == "__main__":
    raise SystemExit(main())
