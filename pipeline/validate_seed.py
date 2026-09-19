"""Check the seed corpus. ``make seed`` reports, ``make seed-strict`` refuses.

BACKLOG E1's acceptance criterion is that an invalid seed is refused *and the
offending field named*, so that is what this prints: one line per problem,
``file: field: what is wrong``, all of them, not the first.

The split between the two modes is the one the message catalogue already uses
(docs/DECISIONS.md D-051, D-079). Structure — the schema, the source
whitelist, duplicate slugs — fails a pull request, because a contributor can
fix it in the same sitting. A Khmer explanation still marked ``[[km:...]]`` is
reported and does not fail, because only a native speaker can clear it and a
build held red until one is hired is a build somebody switches off. ``--strict``
is the M1 gate that refuses those too.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Final

from pipeline.seed_schema import SeedCorpus, SeedProblem, check_seed_file, check_seed_tree

_ROOT: Final = Path(__file__).resolve().parents[1]

#: Where seed files live when no path is given (ARCHITECTURE section 6).
SEED_ROOT: Final = _ROOT / "pipeline" / "seed"


def check(paths: list[Path]) -> SeedCorpus:
    """Validate every given file, and every seed file under every given directory."""
    files = {}
    problems: list[SeedProblem] = []
    for path in paths:
        if path.is_dir():
            corpus = check_seed_tree(path)
            files.update(corpus.files)
            problems.extend(corpus.problems)
            continue
        seed, found = check_seed_file(path)
        problems.extend(found)
        if seed is not None:
            files[path] = seed
    return SeedCorpus(files=files, problems=problems)


def report(corpus: SeedCorpus, *, strict: bool) -> int:
    """Print what was found. Non-zero means do not merge this."""
    for problem in sorted(corpus.problems, key=lambda p: (str(p.path), p.field)):
        print(f"error: {problem}", file=sys.stderr)

    print(f"{len(corpus.files)} seed file(s), {corpus.concept_count} concept(s)")

    for path, slugs in corpus.awaiting_translation.items():
        print(f"{path}: {len(slugs)} concept(s) awaiting a Khmer explanation: {list(slugs)}")

    if corpus.problems:
        return 1
    if strict and corpus.placeholder_total:
        print(
            f"error: {corpus.placeholder_total} Khmer explanation(s) are still "
            "placeholders; this is the M1 gate, not the commit gate "
            "(docs/DEFINITION_OF_DONE.md)",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=[SEED_ROOT],
        help=f"seed files or directories to check (default: {SEED_ROOT})",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="also fail while any Khmer explanation is a placeholder (the M1 gate)",
    )
    arguments = parser.parse_args(argv)
    paths: list[Path] = list(arguments.paths) or [SEED_ROOT]
    return report(check(paths), strict=arguments.strict)


if __name__ == "__main__":
    raise SystemExit(main())
