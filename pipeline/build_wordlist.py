"""Write a level's word list from its transcription table.

BACKLOG E4 / L-11. The table (`<level>.source.tsv`) is what a person typed
from the standard; the list (`<level>.txt`) is what validate.py reads. Keeping
the second derived from the first means a correction has one home, and that a
word nobody transcribed cannot appear in the list.

    make wordlist
"""

from __future__ import annotations

import argparse
from pathlib import Path

from pipeline.wordlist import WORDLIST_DIR, expand_source, read_source, wordlist_path


def build(language: str, level: str, *, directory: Path | None = None) -> tuple[Path, int, int]:
    """Regenerate one list. Returns where it went, rows read, forms written."""
    root = directory or WORDLIST_DIR
    source = root / language / f"{level.lower()}.source.tsv"
    entries = read_source(source)
    forms = expand_source(entries)

    destination = wordlist_path(language, level, directory=root)
    header = [
        f"# Derived from {source.name} by `make wordlist`. Do not edit by hand:",
        "# corrections go into the transcription table, which records the source.",
    ]
    header += [f"# {line}" for line in _source_lines(source)]
    destination.write_text("\n".join([*header, "", *forms]) + "\n", encoding="utf-8")
    return destination, len(entries), len(forms)


def _source_lines(path: Path) -> list[str]:
    return [
        line.strip().removeprefix("#").strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("# source:")
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--language", default="zh")
    parser.add_argument("--level", default="HSK1")
    arguments = parser.parse_args(argv)

    destination, rows, forms = build(arguments.language, arguments.level)
    print(f"{destination}: {forms} form(s) from {rows} table row(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
