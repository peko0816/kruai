"""M0-1: does the scoring provider tell a good speaker from a real beginner?

`docs/REAL_ENVIRONMENT.md` item 3. The decision this answers is not "is the
provider any good" in the abstract — it is whether the numbers it returns can
carry `SCORING_PASS_THRESHOLD`. If a confident speaker and a Cambodian
beginner come back with overlapping scores, no threshold placed between them
means anything, and every attempt a learner makes is graded by noise.

Three questions, and the acceptance for each comes from REAL_ENVIRONMENT:

1. **Is there a gap?** Mean of the good group minus mean of the bad group,
   which has to be at least 15 points.
2. **Do the groups overlap?** At most 20% of samples may fall inside the other
   group's range. A gap between means with heavy overlap is a gap you cannot
   put a threshold in.
3. **Is there phoneme detail, and does it carry tone?** `ZH_TONE_MODE` turns
   on whether the provider's phoneme strings have tone digits in them (ni3)
   or not (ni). Parsing them is cheap; deriving tone from phoneme-level
   accuracy is a different implementation, and E9 cannot be written until
   this is known.

The script never names a vendor. It asks `scoring.registry` for whatever
`SCORING_PROVIDER` says, so running it against FakeScorer exercises every
line of it without a key or a bill — which is how it was written, and how its
tests run.

    make m0-scoring                              # FakeScorer, free, proves the wiring
    SCORING_PROVIDER=azure make m0-scoring ARGS="--confirm-spend"
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import statistics
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from app.core.config import Settings, get_settings
from app.services.scoring.base import AssessMode, Language, PronunciationResult
from app.services.scoring.registry import get_scorer

#: The two groups a sample can belong to. Named rather than boolean because
#: "which group was this in" appears in the report, and `group=False` is not
#: something anybody wants to read there.
GOOD: Final = "good"
BAD: Final = "bad"
GROUPS: Final[tuple[str, ...]] = (GOOD, BAD)

#: Acceptance, from REAL_ENVIRONMENT item 3. Here as constants so the report
#: can state what it was judged against instead of printing bare numbers.
MIN_MEAN_GAP: Final = 15.0
MAX_OVERLAP: Final = 0.20

#: A phoneme string that carries its tone, e.g. ni3 hao3. Azure's zh-CN
#: output may or may not; which one decides how E9 computes tone_score.
_TONE_DIGITS: Final = frozenset("12345")


@dataclass(frozen=True)
class Sample:
    """One recording, with what it was supposed to say and who said it."""

    path: Path
    reference_text: str
    group: str

    @property
    def audio_format(self) -> str:
        return self.path.suffix.lstrip(".").lower() or "wav"


@dataclass(frozen=True)
class Scored:
    sample: Sample
    result: PronunciationResult

    @property
    def score(self) -> float | None:
        return self.result.pron_score


class ManifestError(ValueError):
    """The manifest is unusable. Raised with every problem, not the first."""

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = tuple(problems)


def load_manifest(path: Path) -> list[Sample]:
    """Read the CSV that says what each recording is.

    Columns: ``file``, ``reference_text``, ``group``. Paths are relative to
    the manifest, so a folder of recordings and its manifest move together.

    Every problem is collected before raising. Someone assembling twenty
    recordings by hand will have several wrong, and finding them one run at a
    time is the slowest possible way to learn that.
    """
    rows = list(csv.DictReader(path.read_text(encoding="utf-8-sig").splitlines()))
    problems: list[str] = []
    samples: list[Sample] = []

    if not rows:
        raise ManifestError([f"{path} has no rows"])
    missing = {"file", "reference_text", "group"} - set(rows[0])
    if missing:
        raise ManifestError([f"{path} has no column(s) {sorted(missing)}"])

    for number, row in enumerate(rows, start=2):
        where = f"{path.name}:{number}"
        name = (row.get("file") or "").strip()
        text = (row.get("reference_text") or "").strip()
        group = (row.get("group") or "").strip().lower()
        if not name:
            problems.append(f"{where}: no file")
            continue
        audio = (path.parent / name).resolve()
        if not audio.is_file():
            problems.append(f"{where}: {name} does not exist")
        if not text:
            problems.append(f"{where}: no reference_text; scripted assessment needs one")
        if group not in GROUPS:
            problems.append(f"{where}: group is {group!r}, expected one of {list(GROUPS)}")
            continue
        samples.append(Sample(path=audio, reference_text=text, group=group))

    for group in GROUPS:
        if not any(s.group == group for s in samples):
            problems.append(f"no samples in the {group!r} group; the comparison needs both")
    if problems:
        raise ManifestError(problems)
    return samples


async def score_all(samples: Sequence[Sample], *, settings: Settings) -> list[Scored]:
    """Assess every sample, in order, through whatever provider is configured.

    Sequential on purpose. This runs a few dozen times in total against a free
    tier, and a verification script that needs its own concurrency setting is
    a verification script with one more thing to get wrong.
    """
    scorer = get_scorer(Language.ZH_CN, settings=settings)
    scored: list[Scored] = []
    for sample in samples:
        result = await scorer.assess(
            sample.path.read_bytes(),
            language=Language.ZH_CN,
            mode=AssessMode.SCRIPTED,
            reference_text=sample.reference_text,
            audio_format=sample.audio_format,
        )
        scored.append(Scored(sample=sample, result=result))
    return scored


def overlap(good: Sequence[float], bad: Sequence[float]) -> float:
    """The fraction of samples sitting inside the other group's range.

    Not a distribution test. With twenty recordings there is nothing to fit,
    and the question is the blunt one: can a line be drawn between these two
    sets of numbers? A good speaker scoring below the best beginner is on the
    wrong side of every line that could be drawn.
    """
    if not good or not bad:
        return 1.0
    inside = sum(1 for g in good if g <= max(bad)) + sum(1 for b in bad if b >= min(good))
    return inside / (len(good) + len(bad))


def tone_mode_of(scored: Sequence[Scored]) -> str:
    """What `ZH_TONE_MODE` should be set to, read off the phoneme strings.

    ``parse`` when the provider writes tone into the phoneme itself (ni3), so
    E9 reads it. ``derive`` when it does not, and tone has to be computed from
    phoneme-level accuracy instead. ``unknown`` when no phonemes came back at
    all, which is itself a finding: without them E9 cannot do either.
    """
    phonemes = [p.phoneme for s in scored for w in s.result.words for p in w.phonemes]
    if not phonemes:
        return "unknown"
    if any(set(p) & _TONE_DIGITS for p in phonemes):
        return "parse"
    return "derive"


@dataclass(frozen=True)
class Report:
    scored: tuple[Scored, ...]

    def scores(self, group: str) -> list[float]:
        return [s.score for s in self.scored if s.sample.group == group and s.score is not None]

    @property
    def failed(self) -> list[Scored]:
        return [s for s in self.scored if not s.result.ok or s.score is None]

    @property
    def mean_gap(self) -> float:
        good, bad = self.scores(GOOD), self.scores(BAD)
        if not good or not bad:
            return 0.0
        return statistics.fmean(good) - statistics.fmean(bad)

    @property
    def overlap(self) -> float:
        return overlap(self.scores(GOOD), self.scores(BAD))

    @property
    def tone_mode(self) -> str:
        return tone_mode_of(self.scored)

    @property
    def has_phonemes(self) -> bool:
        return any(w.phonemes for s in self.scored for w in s.result.words)

    @property
    def cost_usd_cents(self) -> int:
        return sum(s.result.cost_usd_cents for s in self.scored)

    @property
    def verdicts(self) -> list[tuple[str, bool, str]]:
        """Each acceptance criterion, whether it passed, and what it measured."""
        return [
            (
                f"mean gap >= {MIN_MEAN_GAP:g}",
                self.mean_gap >= MIN_MEAN_GAP,
                f"{self.mean_gap:.1f} points",
            ),
            (
                f"overlap <= {MAX_OVERLAP:.0%}",
                self.overlap <= MAX_OVERLAP,
                f"{self.overlap:.0%} of samples",
            ),
            (
                "phoneme detail present",
                self.has_phonemes,
                "yes" if self.has_phonemes else "no phonemes returned",
            ),
            (
                "every sample scored",
                not self.failed,
                f"{len(self.failed)} of {len(self.scored)} failed",
            ),
        ]

    @property
    def passed(self) -> bool:
        return all(ok for _, ok, _ in self.verdicts)


def _band(values: Sequence[float]) -> str:
    if not values:
        return "—"
    return f"{statistics.fmean(values):.1f} ({min(values):.1f} to {max(values):.1f})"


def as_markdown(report: Report, *, provider: str) -> str:
    """The report that becomes `docs/m0-1-scoring.md`.

    Written to be read by whoever decides the threshold later, which is why
    every number is next to what it was judged against rather than on its own.
    """
    lines = [
        "# M0-1: pronunciation scoring verification",
        "",
        f"Provider: `{provider}`. "
        f"{len(report.scored)} sample(s), "
        f"{len(report.scores(GOOD))} good / {len(report.scores(BAD))} beginner. "
        f"Estimated cost: {report.cost_usd_cents} USD cents.",
        "",
        "## Acceptance",
        "",
        "| criterion | measured | result |",
        "|---|---|---|",
    ]
    lines += [
        f"| {name} | {detail} | {'pass' if ok else '**FAIL**'} |"
        for name, ok, detail in report.verdicts
    ]
    lines += [
        "",
        f"**{'Passes' if report.passed else 'Does not pass'}** the M0-1 bar "
        "(docs/REAL_ENVIRONMENT.md item 3).",
        "",
        "## Scores",
        "",
        "| group | n | mean (range) |",
        "|---|---|---|",
        f"| good | {len(report.scores(GOOD))} | {_band(report.scores(GOOD))} |",
        f"| beginner | {len(report.scores(BAD))} | {_band(report.scores(BAD))} |",
        "",
        "## ZH_TONE_MODE",
        "",
        f"Set it to `{report.tone_mode}`.",
        "",
        {
            "parse": "The provider writes tone into the phoneme strings, so E9 reads "
            "it rather than computing it.",
            "derive": "The phoneme strings carry no tone, so E9 has to derive "
            "`tone_score` from phoneme-level accuracy.",
            "unknown": "No phonemes came back at all. E9 can do neither until that "
            "is resolved — check the request asks for phoneme-level detail.",
        }[report.tone_mode],
        "",
        "## What this does not settle",
        "",
        "A gap between two groups says a threshold can exist, not where it goes.",
        "Setting `SCORING_PASS_THRESHOLD` means choosing how often a learner who",
        "said it well enough is told they did not, and legacy constraint L-7 has",
        "already shown that number cannot move alone: `MASTERY_DELTA_BASE` and the",
        "three item-type weights are calibrated against it or spaced repetition",
        "stops working at low mastery.",
        "",
        "## Samples",
        "",
        "| group | file | score | reference |",
        "|---|---|---|---|",
    ]
    for s in sorted(report.scored, key=lambda s: (s.sample.group, s.sample.path.name)):
        score = f"{s.score:.1f}" if s.score is not None else f"**{s.result.error_code}**"
        lines.append(
            f"| {s.sample.group} | `{s.sample.path.name}` | {score} | {s.sample.reference_text} |"
        )
    return "\n".join(lines) + "\n"


def as_json(report: Report, *, provider: str) -> dict[str, Any]:
    """The same findings, for anything that would otherwise parse the Markdown."""
    return {
        "provider": provider,
        "samples": len(report.scored),
        "mean_gap": round(report.mean_gap, 2),
        "overlap": round(report.overlap, 4),
        "tone_mode": report.tone_mode,
        "has_phonemes": report.has_phonemes,
        "failed": len(report.failed),
        "passed": report.passed,
        "cost_usd_cents": report.cost_usd_cents,
        "by_group": {
            group: {
                "n": len(report.scores(group)),
                "mean": round(statistics.fmean(report.scores(group)), 2)
                if report.scores(group)
                else None,
            }
            for group in GROUPS
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("manifest", type=Path, help="CSV: file, reference_text, group")
    parser.add_argument("--out", type=Path, default=Path("docs/m0-1-scoring.md"))
    parser.add_argument("--json", type=Path, default=None, help="also write the findings as JSON")
    parser.add_argument(
        "--confirm-spend",
        action="store_true",
        help="required when the configured provider is not 'fake'; assessment is billed",
    )
    arguments = parser.parse_args(argv)

    settings = get_settings()
    # CLAUDE.md section 6: a paid call is one of the four things that stop and
    # ask, and the difference between a free run and a billed one is one line
    # in .env that nobody re-reads (same guard as generate.py, D-085).
    if settings.scoring_provider != "fake" and not arguments.confirm_spend:
        print(
            f"error: SCORING_PROVIDER={settings.scoring_provider!r} bills per call. "
            "Re-run with --confirm-spend once you have agreed the budget.",
            file=sys.stderr,
        )
        return 2

    try:
        samples = load_manifest(arguments.manifest)
    except (OSError, ManifestError) as error:
        for problem in getattr(error, "problems", [str(error)]):
            print(f"error: {problem}", file=sys.stderr)
        return 1

    report = Report(tuple(asyncio.run(score_all(samples, settings=settings))))
    provider = settings.scoring_provider

    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    arguments.out.write_text(as_markdown(report, provider=provider), encoding="utf-8")
    if arguments.json is not None:
        arguments.json.write_text(
            json.dumps(as_json(report, provider=provider), indent=2) + "\n", encoding="utf-8"
        )

    for name, ok, detail in report.verdicts:
        print(f"  {'pass' if ok else 'FAIL'}  {name}: {detail}")
    print(f"ZH_TONE_MODE should be {report.tone_mode!r}")
    print(f"{arguments.out}: written ({report.cost_usd_cents} usd cents)")
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
