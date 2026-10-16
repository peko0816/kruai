"""Hand the message catalogue to a translator, and take it back.

`make i18n` says how many strings are still waiting; this is how they stop
waiting. Export writes a spreadsheet, the translator fills one column, apply
merges it back and refuses anything that would break at runtime.

Two of the checks on the way back in are the point of doing this with a tool
rather than by hand:

**Parameters have to survive.** ``bot.quota_exhausted`` contains
``{resets_at}``. A translation that drops it raises at render time, in front of
a learner, in one language only — and the person best placed to drop it is a
translator who cannot read the code around it. So the export names the
parameters and apply refuses a translation that loses one.

**Khmer has to be Khmer.** A row left in English looks finished and reads as
untranslated, which is exactly the failure the placeholder convention exists to
prevent (docs/DECISIONS.md D-050).

    make i18n-export                 # locales/km.todo.csv
    make i18n-apply FILE=...         # merges it back, then `make i18n`
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from bot import i18n, i18n_check

#: Same as the review export, for the same reason: Excel reads a UTF-8 file
#: without a byte-order mark as the local code page, and the person it happens
#: to is a translator (docs/DECISIONS.md D-093).
ENCODING: Final = "utf-8-sig"

COLUMNS: Final[tuple[str, ...]] = ("key", "english", "parameters", "used_at", "translation")

#: ``{resets_at}`` and friends. str.format names, which is what i18n.t uses.
_PARAMETER: Final = re.compile(r"\{(\w+)\}")

#: Khmer, U+1780-U+17FF (CODING_STANDARDS section 11).
_KHMER: Final = re.compile(r"[\u1780-\u17ff]")

#: Han, for the Chinese catalogue.
_HAN: Final = re.compile(r"[\u4e00-\u9fff]")

#: What a finished translation has to contain, per locale. English has none —
#: it is the source — and a locale added later needs a row here, which is the
#: point of making it a table rather than an `if`.
SCRIPTS: Final[dict[str, re.Pattern[str]]] = {"km": _KHMER, "zh": _HAN}


@dataclass(frozen=True)
class Row:
    key: str
    english: str
    parameters: str
    used_at: str
    translation: str = ""


def parameters_of(text: str) -> tuple[str, ...]:
    return tuple(sorted(set(_PARAMETER.findall(text))))


def usage_sites() -> dict[str, str]:
    """key -> where the code uses it, so a translator can tell a button from a sentence."""
    sites: dict[str, list[str]] = {}
    for path in i18n_check.python_files():
        for lineno, key in i18n_check.keys_used_in(path):
            sites.setdefault(key, []).append(f"{path.name}:{lineno}")
    return {key: ", ".join(sorted(set(where))) for key, where in sites.items()}


def pending(locale: str) -> list[str]:
    """Keys still carrying a placeholder, in catalogue order."""
    catalogue = i18n.catalogue(locale)
    return [key for key, value in catalogue.items() if i18n.is_placeholder(value)]


def rows_for(locale: str, *, everything: bool = False) -> list[Row]:
    english = i18n.catalogue("en")
    catalogue = i18n.catalogue(locale)
    sites = usage_sites()
    wanted = sorted(catalogue) if everything else sorted(pending(locale))
    return [
        Row(
            key=key,
            english=english.get(key, ""),
            parameters=" ".join(parameters_of(english.get(key, ""))),
            used_at=sites.get(key, ""),
            # A finished row comes back out so a reviewer can see it in context,
            # but a placeholder must not: it would read as a suggestion.
            translation="" if i18n.is_placeholder(catalogue[key]) else catalogue[key],
        )
        for key in wanted
    ]


#: Marks a row that is guidance rather than work. Rows are skipped on the way
#: back in by this, not by position: a translator who sorts the sheet by any
#: column scatters the preamble into the data, and that must not break the
#: merge or silently become a translation.
COMMENT: Final = "#"


def preamble(locale: str, count: int) -> list[str]:
    """What the person filling the sheet needs, inside the sheet.

    It used to be printed by `make i18n-export` instead, which reaches whoever
    ran the command and nobody after that — the file is then emailed on, and
    the instructions are not in it. In English because that is the language
    REAL_ENVIRONMENT asks a translator for; Khmer guidance written by this
    pipeline would be the one piece of unreviewed Khmer in the whole handoff.
    """
    return [
        f"{COMMENT} {count} user-facing strings for a '{locale}' translator.",
        f"{COMMENT} Fill in the LAST column — 'translation'. Change nothing else.",
        f"{COMMENT} 'key' is how your work is matched back — leave it exactly as it is.",
        f"{COMMENT} Any {{word}} in curly braces is a value the app fills in at runtime.",
        f"{COMMENT}   Keep every one of them spelled exactly the same — otherwise",
        f"{COMMENT}   the app crashes for users in this language only. You may",
        f"{COMMENT}   move them to wherever the sentence needs them.",
        f"{COMMENT} 'used_at' says where the string appears — so you can tell a button",
        f"{COMMENT}   from a sentence. 'english' is the original.",
        f"{COMMENT} Leave a row blank if you are unsure. A half-filled sheet is fine",
        f"{COMMENT}   and can be sent back as it is.",
        f"{COMMENT} Save as CSV with UTF-8 encoding. Do not save as .xlsx.",
        f"{COMMENT} Lines starting with {COMMENT} are these notes; they are ignored.",
    ]


def write_csv(rows: Iterable[Row], path: Path, *, locale: str = "km") -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = list(rows)
    written = 0
    with path.open("w", encoding=ENCODING, newline="") as handle:
        notes = csv.writer(handle)
        for line in preamble(locale, len(body)):
            notes.writerow([line])
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for row in body:
            writer.writerow(
                {
                    "key": row.key,
                    "english": row.english,
                    "parameters": row.parameters,
                    "used_at": row.used_at,
                    "translation": row.translation,
                }
            )
            written += 1
    return written


# ------------------------------------------------------------------- reading


@dataclass(frozen=True)
class Problem:
    key: str
    message: str

    def __str__(self) -> str:
        return f"{self.key}: {self.message}"


def check_translations(
    filled: dict[str, str], *, locale: str
) -> tuple[dict[str, str], list[Problem]]:
    """Split a filled sheet into what can be applied and what cannot."""
    english = i18n.catalogue("en")
    catalogue = i18n.catalogue(locale)
    script = SCRIPTS.get(locale)

    good: dict[str, str] = {}
    problems: list[Problem] = []

    for key, text in filled.items():
        text = text.strip()
        if not text:
            continue  # not translated yet; leaving it is not an error
        if key not in catalogue:
            problems.append(Problem(key, "no such message key; was it renamed?"))
            continue
        if i18n.is_placeholder(text):
            problems.append(Problem(key, "the placeholder was copied instead of translated"))
            continue
        missing = set(parameters_of(english.get(key, ""))) - set(parameters_of(text))
        if missing:
            problems.append(
                Problem(
                    key,
                    f"loses the parameter(s) {sorted(missing)}; without them this raises "
                    f"in front of a learner, in {locale} only",
                )
            )
            continue
        if script is not None and not script.search(text):
            problems.append(
                Problem(key, f"is not written in {locale}; an untranslated row reads as finished")
            )
            continue
        good[key] = text

    return good, problems


def read_csv(path: Path) -> dict[str, str]:
    """Read a filled sheet back, past whatever the preamble has become.

    The header is found rather than assumed to be line one, and note rows are
    recognised by their leading ``#`` rather than by where they sit, because a
    sheet comes back sorted, re-saved and occasionally with rows dragged about.
    """
    with path.open(encoding=ENCODING, newline="") as handle:
        rows = list(csv.reader(handle))

    header: list[str] | None = None
    body: list[list[str]] = []
    for row in rows:
        if header is None:
            if row and row[0].strip() == COLUMNS[0]:
                header = [cell.strip() for cell in row]
            continue
        body.append(row)

    if header is None:
        raise ValueError(f"{path} has no 'key' column; is it the exported sheet?")
    if "translation" not in header:
        raise ValueError(f"{path} has no 'translation' column")

    key_at, translation_at = header.index("key"), header.index("translation")
    filled: dict[str, str] = {}
    for row in body:
        if len(row) <= key_at:
            continue
        key = row[key_at].strip()
        if not key or key.startswith(COMMENT):
            continue
        filled[key] = row[translation_at] if len(row) > translation_at else ""
    return filled


def apply(translations: dict[str, str], *, locale: str) -> int:
    """Merge into the catalogue on disk. Returns how many keys changed."""
    path = i18n.LOCALES_DIR / f"{locale}.json"
    catalogue = json.loads(path.read_text(encoding="utf-8"))
    changed = 0
    for key, text in translations.items():
        if catalogue.get(key) != text:
            catalogue[key] = text
            changed += 1
    path.write_text(
        json.dumps(catalogue, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    i18n.catalogue.cache_clear()
    return changed


# ----------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    out = sub.add_parser("export", help="write the sheet a translator fills in")
    out.add_argument("--locale", default="km", choices=sorted(SCRIPTS))
    out.add_argument("--out", type=Path, default=None)
    out.add_argument("--all", action="store_true", help="include keys that are already translated")

    back = sub.add_parser("apply", help="merge a filled sheet back into the catalogue")
    back.add_argument("file", type=Path)
    back.add_argument("--locale", default="km", choices=sorted(SCRIPTS))

    arguments = parser.parse_args(argv)

    if arguments.command == "export":
        rows = rows_for(arguments.locale, everything=arguments.all)
        destination = arguments.out or i18n.LOCALES_DIR / f"{arguments.locale}.todo.csv"
        count = write_csv(rows, destination, locale=arguments.locale)
        print(f"{destination}: {count} string(s) for a {arguments.locale} translator")
        print("Fill in the 'translation' column only. Keep every {parameter} exactly as it is.")
        return 0

    try:
        filled = read_csv(arguments.file)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    good, problems = check_translations(filled, locale=arguments.locale)
    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)

    changed = apply(good, locale=arguments.locale) if good else 0
    still = len(pending(arguments.locale))
    print(f"{arguments.locale}: {changed} string(s) updated, {still} still waiting")
    if problems:
        print(
            f"error: {len(problems)} row(s) were not applied; fix them in the sheet and re-run",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
