"""Stage [4]: pull a sample out of a draft for a native speaker to judge.

BACKLOG E5, PRD 6.1. validate.py can tell whether the Khmer is Khmer; it
cannot tell whether it is *good* Khmer, and that is what M1 turns on — the
acceptance criterion is a native-speaker pass rate of 90% on a 10% sample
(docs/DEFINITION_OF_DONE.md).

Three things about the sample are decisions rather than mechanics.

**It is stratified, and the Khmer explanations are their own stratum**
(docs/DECISIONS.md D-081). A concept has one explanation, eight sentences,
twelve substitutions and three dialogues, so sampling 10% of everything in one
pool would hand a reviewer mostly substitutions — a dozen words — and let a
90% pass rate be reached without anybody reading a paragraph of machine-written
Khmer prose. That is exactly the material with the most to go wrong in it.

**It is deterministic.** The seed comes from the draft's own identity, so two
people who export the same draft review the same rows, and a verdict can be
traced back to the row it was given for. ``--seed`` draws a different sample
when a second round is wanted.

**It is at least one row per stratum.** Ten percent of three dialogues is
nought, and a stratum nobody looks at is a stratum with no evidence behind it.
"""

from __future__ import annotations

import argparse
import csv
import math
import random
import sys
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

from app.core.config import get_settings
from pipeline.draft import ConceptDraft, Draft

#: What a reviewer is asked about, one stratum each. Ordered as the exported
#: file reads: the prose first, because it is the hardest to get right.
STRATA: Final[tuple[str, ...]] = (
    "km_explanation",
    "target_sentence",
    "substitution",
    "dialogue",
)

#: Excel on Windows reads a UTF-8 file without a byte-order mark as the local
#: code page, which turns every Khmer and Chinese column into mojibake — and
#: the person it happens to is a translator, who will report the file as broken
#: rather than as mis-encoded. utf-8-sig costs three bytes (D-093).
ENCODING: Final = "utf-8-sig"

#: The columns, in order. `id` is first because it is what a returned verdict
#: is matched on; `verdict` and `comment` are last because they are the two the
#: reviewer types into.
COLUMNS: Final[tuple[str, ...]] = (
    "id",
    "kind",
    "concept",
    "pattern",
    "chinese",
    "pinyin",
    "khmer",
    "verdict",
    "comment",
)

#: What a reviewer may write in `verdict`. Three values, not two: "understandable
#: but nobody would say it that way" is the judgement a native speaker is for,
#: and it is not the same as wrong.
VERDICTS: Final[tuple[str, ...]] = ("ok", "awkward", "wrong")


@dataclass(frozen=True)
class Row:
    """One thing to look at."""

    id: str
    kind: str
    concept: str
    pattern: str
    chinese: str
    pinyin: str
    khmer: str
    verdict: str = ""
    comment: str = ""


def rows_of(concept: ConceptDraft) -> Iterator[Row]:
    """Every reviewable item in one concept, tagged with its stratum."""
    yield Row(
        id=f"{concept.slug}/km_explanation",
        kind="km_explanation",
        concept=concept.slug,
        pattern=concept.pattern,
        chinese="",
        pinyin="",
        khmer=concept.km_explanation,
    )
    for index, sentence in enumerate(concept.target_sentences):
        yield Row(
            id=f"{concept.slug}/target_sentences[{index}]",
            kind="target_sentence",
            concept=concept.slug,
            pattern=concept.pattern,
            chinese=sentence.zh,
            pinyin=sentence.pinyin,
            khmer=sentence.km_gloss,
        )
    for index, substitution in enumerate(concept.substitutions):
        yield Row(
            id=f"{concept.slug}/substitutions[{index}]",
            kind="substitution",
            concept=concept.slug,
            pattern=concept.pattern,
            chinese=substitution.zh,
            pinyin=substitution.pinyin,
            khmer=substitution.km_gloss,
        )
    for index, dialogue in enumerate(concept.dialogues):
        yield Row(
            id=f"{concept.slug}/dialogues[{index}]",
            kind="dialogue",
            concept=concept.slug,
            pattern=concept.pattern,
            # Both halves, because the question and the model answer are judged
            # together: a good answer to a question nobody would ask is not a
            # usable turn.
            chinese=f"{dialogue.prompt_zh} → {dialogue.sample_answer_zh}",
            pinyin="",
            khmer=dialogue.prompt_km,
        )


def sample_size(population: int, rate: float) -> int:
    """How many rows to draw. Rounds up, and never rounds a stratum away.

    Rounding up rather than down because the rate is a floor in the acceptance
    criterion, not a target; and a non-empty stratum always yields at least one
    row, so that every kind of content has some evidence behind it.
    """
    if population <= 0 or rate <= 0:
        return 0
    return min(population, max(1, math.ceil(population * rate)))


def select(draft: Draft, *, rate: float, seed: int | None = None) -> list[Row]:
    """The sample, stratified by kind and in draft order.

    Deterministic: with no explicit seed, the draw depends only on the draft's
    language, level and the rate, so the same draft exported twice gives the
    same rows to review.
    """
    everything = [row for concept in draft.concepts for row in rows_of(concept)]
    # Not hash(): Python randomises string hashing per process, so a seed
    # derived from it would give a different sample on every run — the same
    # trap minhash.py sidesteps. random.Random seeds from a string via SHA-512,
    # which is stable across processes and versions.
    key = str(seed) if seed is not None else f"{draft.language}/{draft.level}/{rate:.6f}"
    chosen: set[str] = set()

    for stratum in STRATA:
        population = [row for row in everything if row.kind == stratum]
        wanted = sample_size(len(population), rate)
        if not wanted:
            continue
        # A separate generator per stratum, so that changing the size of one
        # stratum does not reshuffle the others.
        rng = random.Random(f"{key}:{stratum}")
        chosen.update(row.id for row in rng.sample(population, wanted))

    return [row for row in everything if row.id in chosen]


def write_csv(rows: Sequence[Row], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding=ENCODING, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def instructions(path: Path, rows: Sequence[Row]) -> str:
    """What to say when sending the file. Printed, not written into the CSV.

    A CSV with a preamble is a CSV that no spreadsheet opens correctly, so the
    guidance goes to whoever is sending the file rather than into the file.
    """
    by_kind = {stratum: sum(1 for row in rows if row.kind == stratum) for stratum in STRATA}
    counts = ", ".join(f"{kind}: {count}" for kind, count in by_kind.items() if count)
    return "\n".join(
        [
            f"{path}: {len(rows)} row(s) to review ({counts})",
            "",
            "Send with these instructions:",
            "  · Fill in 'verdict' for every row: "
            + " / ".join(VERDICTS)
            + " — 'awkward' means understandable but not what a Khmer speaker would say.",
            "  · Put the correction in 'comment' whenever the verdict is not 'ok'.",
            "  · Do not edit any other column; 'id' is how the answers are matched back.",
            "  · Open it in a spreadsheet, not a text editor, and keep it as CSV.",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("draft", type=Path, help="the draft JSON written by generate.py")
    parser.add_argument("--out", type=Path, default=None, help="where to write the CSV")
    parser.add_argument(
        "--rate",
        type=float,
        default=None,
        help="fraction of each stratum to draw (default: PIPELINE_REVIEW_SAMPLE_RATE)",
    )
    parser.add_argument(
        "--seed", type=int, default=None, help="draw a different sample of the same size"
    )
    arguments = parser.parse_args(argv)

    settings = get_settings()
    rate = arguments.rate if arguments.rate is not None else settings.pipeline_review_sample_rate
    if not 0.0 <= rate <= 1.0:
        print(f"error: --rate must be between 0 and 1, got {rate}", file=sys.stderr)
        return 2

    try:
        draft = Draft.model_validate_json(arguments.draft.read_text(encoding="utf-8"))
    except OSError as error:
        print(f"error: cannot read {arguments.draft}: {error.strerror or error}", file=sys.stderr)
        return 1

    rows = select(draft, rate=rate, seed=arguments.seed)
    out = arguments.out or arguments.draft.with_suffix("").with_suffix(".review.csv")
    write_csv(rows, out)
    print(instructions(out, rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
