"""M0-2: does the Khmer voice actually sound like Khmer, and which one?

`docs/REAL_ENVIRONMENT.md` item 5. The plan was three vendors. Checked against
their own voice lists (D-125), only Azure has Khmer text-to-speech at all —
Google's table goes from Kannada to Korean, ElevenLabs' from Kazakh to Kirghiz.
So the question moved: not which vendor, but whether Azure's Khmer clears the
bar, and which of its voices reads the narration and which the feedback.

What is compared is a *candidate*: a provider, optionally pinned to one of its
vendor voices — ``azure:km-KH-SreymomNeural``. Three vendors and two voices of
one vendor are the same experiment as far as the blinding is concerned, so the
machinery did not change; only the unit it shuffles did.

The blinding is the part that has to be built rather than promised:

* **Nothing identifying is in the filename.** Clips are named by a digest, and
  the key that maps a digest back to a candidate is written to a separate file
  the reviewer never receives.
* **The order is shuffled per passage.** Clips in a fixed order are not blind —
  a listener notices by the third passage that the first is always the same
  voice, and from then on is rating a voice rather than a clip.
* **The shuffle is seeded from the passage**, so two people exporting the
  same material get the same sheet, and a score can be traced to the clip it
  was given for.

Two commands, because they happen days apart:

    make m0-tts                    # synthesise + write the sheet and the key
    make m0-tts-score SHEETS=...   # un-blind the filled sheets and decide
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import random
import statistics
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.core.config import Settings, get_settings
from app.services.tts.base import Voice
from app.services.tts.registry import build_tts

#: What a reviewer scores each clip on, 1-5. Two axes rather than one overall
#: mark: a voice can be perfectly clear and still sound like a machine, and
#: those two faults have different consequences for a course.
AXES: Final[tuple[str, ...]] = ("naturalness", "intelligibility")

#: REAL_ENVIRONMENT item 5: both means at or above 4, from two or more
#: reviewers independently, with no fatal pronunciation error.
MIN_MEAN: Final = 4.0
MIN_REVIEWERS: Final = 2

SHEET_COLUMNS: Final[tuple[str, ...]] = (
    "clip",
    "passage",
    "khmer",
    *AXES,
    "fatal_error",
    "comment",
)

KEY_COLUMNS: Final[tuple[str, ...]] = ("clip", "passage", "candidate", "provider", "resolved_voice")


@dataclass(frozen=True)
class Passage:
    """One piece of Khmer, and where it came from.

    `source` is not decoration. R7 means every piece of third-party text in
    this repository can say where it came from, and a blind-test corpus is
    third-party text like any other.
    """

    id: str
    text: str
    source: str


@dataclass(frozen=True)
class Candidate:
    """What a reviewer is, without knowing it, choosing between.

    A provider, and optionally the vendor voice it is pinned to. With only one
    vendor offering Khmer, comparing vendors stopped being the question;
    comparing that vendor's voices is the same experiment one level down.
    """

    provider: str
    voice: str = ""

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.voice}" if self.voice else self.provider

    @classmethod
    def parse(cls, text: str) -> Candidate:
        provider, _, voice = text.strip().partition(":")
        return cls(provider=provider.strip(), voice=voice.strip())


@dataclass(frozen=True)
class Clip:
    passage: Passage
    candidate: Candidate
    resolved_voice: str
    audio: bytes
    audio_format: str
    cost_usd_cents: int

    @property
    def name(self) -> str:
        """A filename that says nothing about who made it.

        Digested from the passage and the candidate so it is stable across
        runs — a reviewer who is sent the sheet twice gets the same names —
        while carrying neither in a readable form.
        """
        digest = hashlib.sha256(f"{self.passage.id}/{self.candidate.label}".encode()).hexdigest()
        return f"clip-{digest[:10]}.{self.audio_format}"


class CorpusError(ValueError):
    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = tuple(problems)


def load_corpus(path: Path) -> list[Passage]:
    """Read the passages to synthesise.

    Columns: ``id``, ``khmer``, ``source``. Every row needs a source; a
    corpus that cannot say where its text came from cannot be used (R7, and
    `seed_sources.py` takes the same line about content).
    """
    rows = list(csv.DictReader(path.read_text(encoding="utf-8-sig").splitlines()))
    if not rows:
        raise CorpusError([f"{path} has no rows"])
    missing = {"id", "khmer", "source"} - set(rows[0])
    if missing:
        raise CorpusError([f"{path} has no column(s) {sorted(missing)}"])

    problems: list[str] = []
    passages: list[Passage] = []
    seen: set[str] = set()
    for number, row in enumerate(rows, start=2):
        where = f"{path.name}:{number}"
        pid = (row.get("id") or "").strip()
        text = (row.get("khmer") or "").strip()
        source = (row.get("source") or "").strip()
        if not pid:
            problems.append(f"{where}: no id")
            continue
        if pid in seen:
            problems.append(f"{where}: id {pid!r} is used twice")
        seen.add(pid)
        if not text:
            problems.append(f"{where}: no khmer text")
        elif not any("ក" <= c <= "៿" for c in text):
            problems.append(f"{where}: {pid!r} has no Khmer script in it")
        if not source:
            problems.append(f"{where}: no source; R7 needs every passage to say where it is from")
        if pid and text and source:
            passages.append(Passage(id=pid, text=text, source=source))
    if problems:
        raise CorpusError(problems)
    return passages


async def synthesise(
    passages: Sequence[Passage], *, candidates: Sequence[Candidate], settings: Settings
) -> tuple[list[Clip], list[str]]:
    """One clip per passage per candidate. Failures are reported, not raised.

    A pinned candidate builds its provider with that voice standing behind
    KM_NARRATOR, through the same registry every runtime caller uses — the
    test listens to exactly what a pack build would produce, not to a voice
    reached some other way.
    """
    clips: list[Clip] = []
    failures: list[str] = []
    for candidate in candidates:
        tts = build_tts(
            candidate.provider,
            voices={Voice.KM_NARRATOR: candidate.voice} if candidate.voice else None,
        )
        for passage in passages:
            result = await tts.synthesize(
                passage.text,
                voice=Voice.KM_NARRATOR,
                speaking_rate=settings.tts_speaking_rate_km,
            )
            if not result.ok:
                failures.append(f"{candidate.label}/{passage.id}: {result.error_code}")
                continue
            clips.append(
                Clip(
                    passage=passage,
                    candidate=candidate,
                    resolved_voice=result.resolved_voice,
                    audio=result.audio,
                    audio_format=result.audio_format,
                    cost_usd_cents=result.cost_usd_cents,
                )
            )
    return clips, failures


def shuffled(clips: Sequence[Clip], *, seed: str) -> list[Clip]:
    """Clips grouped by passage, each group in its own shuffled order.

    Grouped so a reviewer hears the same sentence each way in a row, which
    is what makes a comparison possible at all. Shuffled within the group
    because a fixed order stops being blind by the third passage.
    """
    out: list[Clip] = []
    for passage_id in dict.fromkeys(c.passage.id for c in clips):
        group = [c for c in clips if c.passage.id == passage_id]
        random.Random(f"{seed}/{passage_id}").shuffle(group)
        out += group
    return out


def write_sheet(clips: Sequence[Clip], path: Path) -> None:
    """What a reviewer fills in. Carries no vendor, by construction."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SHEET_COLUMNS)
        writer.writeheader()
        for clip in clips:
            writer.writerow(
                {
                    "clip": clip.name,
                    "passage": clip.passage.id,
                    "khmer": clip.passage.text,
                    **dict.fromkeys(AXES, ""),
                    "fatal_error": "",
                    "comment": "",
                }
            )


def write_key(clips: Sequence[Clip], path: Path) -> None:
    """The mapping back to vendors. Never sent out with the clips.

    Separate file rather than a column nobody is supposed to read: a column
    gets read, and a reviewer who knows which one is which is no longer
    giving an independent opinion.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=KEY_COLUMNS)
        writer.writeheader()
        for clip in clips:
            writer.writerow(
                {
                    "clip": clip.name,
                    "passage": clip.passage.id,
                    "candidate": clip.candidate.label,
                    "provider": clip.candidate.provider,
                    "resolved_voice": clip.resolved_voice,
                }
            )


# --------------------------------------------------------------------- scoring


@dataclass(frozen=True)
class Verdict:
    candidate: str
    means: dict[str, float]
    fatal: int
    reviewers: int

    @property
    def passed(self) -> bool:
        return (
            self.reviewers >= MIN_REVIEWERS
            and self.fatal == 0
            and all(m >= MIN_MEAN for m in self.means.values())
        )


def tally(
    key: Sequence[dict[str, str]], sheets: Sequence[Sequence[dict[str, str]]]
) -> list[Verdict]:
    """Un-blind the filled sheets and score each candidate.

    Takes the sheets as separate sequences rather than one concatenated list
    because "how many people rated this" is part of the acceptance, and
    pooling the rows first throws that away.
    """
    candidate_of = {row["clip"]: row["candidate"] for row in key}
    scores: dict[str, dict[str, list[float]]] = {}
    fatal: dict[str, int] = {}
    raters: dict[str, set[int]] = {}

    for number, sheet in enumerate(sheets):
        for row in sheet:
            candidate = candidate_of.get(row.get("clip", ""))
            if candidate is None:
                continue
            bucket = scores.setdefault(candidate, {axis: [] for axis in AXES})
            rated = False
            for axis in AXES:
                raw = (row.get(axis) or "").strip()
                if raw:
                    bucket[axis].append(float(raw))
                    rated = True
            if (row.get("fatal_error") or "").strip().lower() in {"y", "yes", "1", "true"}:
                fatal[candidate] = fatal.get(candidate, 0) + 1
            if rated:
                raters.setdefault(candidate, set()).add(number)

    return [
        Verdict(
            candidate=candidate,
            means={
                axis: statistics.fmean(values) if values else 0.0 for axis, values in bucket.items()
            },
            fatal=fatal.get(candidate, 0),
            reviewers=len(raters.get(candidate, set())),
        )
        for candidate, bucket in sorted(scores.items())
    ]


def as_markdown(verdicts: Sequence[Verdict]) -> str:
    """The report that becomes `docs/tts-decision.md`."""
    winners = [v for v in verdicts if v.passed]
    best = max(winners, key=lambda v: statistics.fmean(v.means.values()), default=None)
    lines = [
        "# M0-2: Khmer voice selection",
        "",
        f"Blind listening test, {len(verdicts)} candidate voice(s). "
        f"Acceptance: both means >= {MIN_MEAN:g} from >= {MIN_REVIEWERS} independent "
        "reviewers, with no fatal pronunciation error "
        "(docs/REAL_ENVIRONMENT.md item 5).",
        "",
        "| candidate | " + " | ".join(AXES) + " | fatal | reviewers | result |",
        "|---" * (len(AXES) + 4) + "|",
    ]
    for v in verdicts:
        cells = " | ".join(f"{v.means[a]:.2f}" for a in AXES)
        lines.append(
            f"| `{v.candidate}` | {cells} | {v.fatal} | {v.reviewers} | "
            f"{'pass' if v.passed else '**fail**'} |"
        )
    lines += [
        "",
        f"**Choose `{best.candidate}`.**" if best else "**No candidate passes.**",
        "",
    ]
    if best is None:
        lines += [
            "Nothing here clears the bar, and of the three vendors planned only one",
            "has Khmer at all (D-125), so there is no runner-up to fall back on.",
            "The options are a source outside the original plan, or accepting a",
            "lower bar — and the second of those is a product decision about what",
            "a learner hears, not a technical one.",
            "",
        ]
    narrator = Candidate.parse(best.candidate).voice if best else ""
    lines += [
        "## Voice mapping",
        "",
        "| logical voice | vendor voice |",
        "|---|---|",
        f"| `km_narrator` | {f'`{narrator}`' if narrator else '_fill in_'} |",
        "| `km_feedback` | _the narrator's voice, or the runner-up if it also passed_ |",
        "",
        "E8 reads this table. Until it is filled in, `TTS_PROVIDER` has nothing",
        "to resolve a logical voice to.",
    ]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------- CLI


def _export(arguments: argparse.Namespace, settings: Settings) -> int:
    try:
        passages = load_corpus(arguments.corpus)
    except (OSError, CorpusError) as error:
        for problem in getattr(error, "problems", [str(error)]):
            print(f"error: {problem}", file=sys.stderr)
        return 1

    candidates = [Candidate.parse(c) for c in arguments.candidates.split(",") if c.strip()]
    clips, failures = asyncio.run(synthesise(passages, candidates=candidates, settings=settings))
    for failure in failures:
        print(f"error: {failure}", file=sys.stderr)
    if not clips:
        print("error: nothing synthesised", file=sys.stderr)
        return 1

    ordered = shuffled(clips, seed=arguments.seed)
    arguments.out.mkdir(parents=True, exist_ok=True)
    for clip in ordered:
        (arguments.out / clip.name).write_bytes(clip.audio)
    write_sheet(ordered, arguments.out / "scoring-sheet.csv")
    write_key(ordered, arguments.key)

    spent = sum(c.cost_usd_cents for c in clips)
    print(
        f"{arguments.out}: {len(ordered)} clip(s) from {len(candidates)} candidate(s), "
        f"{spent} usd cents"
    )
    print(f"{arguments.key}: the mapping back to vendors — do NOT send this with the clips")
    print(f"Send the folder and scoring-sheet.csv to {MIN_REVIEWERS}+ reviewers, separately.")
    return 0 if not failures else 1


def _score(arguments: argparse.Namespace) -> int:
    key = list(csv.DictReader(arguments.key.read_text(encoding="utf-8-sig").splitlines()))
    sheets = [
        list(csv.DictReader(path.read_text(encoding="utf-8-sig").splitlines()))
        for path in arguments.sheets
    ]
    if len(sheets) < MIN_REVIEWERS:
        print(
            f"error: {len(sheets)} sheet(s); the acceptance needs {MIN_REVIEWERS} "
            "independent reviewers",
            file=sys.stderr,
        )
        return 2
    verdicts = tally(key, sheets)
    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    arguments.out.write_text(as_markdown(verdicts), encoding="utf-8")
    for v in verdicts:
        marks = " ".join(f"{a}={v.means[a]:.2f}" for a in AXES)
        print(f"  {'pass' if v.passed else 'FAIL'}  {v.candidate}: {marks} fatal={v.fatal}")
    print(f"{arguments.out}: written")
    return 0 if any(v.passed for v in verdicts) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    out = sub.add_parser("export", help="synthesise the clips and write the blind sheet")
    out.add_argument("corpus", type=Path, help="CSV: id, khmer, source")
    out.add_argument(
        "--candidates",
        default="fake",
        help="comma-separated provider[:vendor_voice], e.g. "
        "azure:km-KH-SreymomNeural,azure:km-KH-PisethNeural",
    )
    out.add_argument("--out", type=Path, default=Path("m0/tts-blind"))
    out.add_argument("--key", type=Path, default=Path("m0/tts-blind-key.csv"))
    out.add_argument("--seed", default="m0-2", help="changes the shuffle for a second round")
    out.add_argument(
        "--confirm-spend",
        action="store_true",
        help="required when any provider is not 'fake'; synthesis is billed per character",
    )

    back = sub.add_parser("score", help="un-blind the filled sheets and decide")
    back.add_argument("sheets", type=Path, nargs="+", help="one filled sheet per reviewer")
    back.add_argument("--key", type=Path, default=Path("m0/tts-blind-key.csv"))
    back.add_argument("--out", type=Path, default=Path("docs/tts-decision.md"))

    arguments = parser.parse_args(argv)
    settings = get_settings()

    if arguments.command == "score":
        return _score(arguments)

    billed = sorted(
        {
            Candidate.parse(c).provider
            for c in arguments.candidates.split(",")
            if c.strip() and Candidate.parse(c).provider != "fake"
        }
    )
    # Section 6 again: synthesis is billed per character, and every candidate
    # re-synthesises the whole corpus — two voices is two bills, not one.
    if billed and not arguments.confirm_spend:
        print(
            f"error: {billed} bill per character. Re-run with --confirm-spend "
            "once you have agreed the budget.",
            file=sys.stderr,
        )
        return 2
    return _export(arguments, settings)


if __name__ == "__main__":
    raise SystemExit(main())
