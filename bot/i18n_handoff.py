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

#: Each target locale's own script. Used to catch a row filled in for the
#: wrong sheet, not to require the script be present — see is_untranslated.
SCRIPTS: Final[dict[str, re.Pattern[str]]] = {
    "km": re.compile(r"[\u1780-\u17ff]"),  # Khmer
    "zh": re.compile(r"[\u4e00-\u9fff]"),  # Han
}


@dataclass(frozen=True)
class Row:
    key: str
    english: str
    parameters: str
    used_at: str
    translation: str = ""


def parameters_of(text: str) -> tuple[str, ...]:
    return tuple(sorted(set(_PARAMETER.findall(text))))


def _without_parameters(text: str) -> str:
    return " ".join(_PARAMETER.sub(" ", text).split())


def has_translatable_words(english: str) -> bool:
    """Is there anything in this string to translate at all?

    ``bot.explain`` is the whole string ``{text}`` — a slot the app fills with
    content that is already in the learner's language. The only correct
    "translation" of it is itself.
    """
    return any(character.isalpha() for character in _without_parameters(english))


def wrong_language(text: str, *, locale: str) -> str | None:
    """The locale this text looks like it was written for, if not ``locale``.

    Asked this way round on purpose. Requiring the target script rejects
    correct work — ``/ {limit}`` is a complete Chinese rendering of
    ``of {limit}`` and has no Han in it (D-118). But text carrying *another*
    target's script and none of its own is a row filled into the wrong sheet,
    which is an easy mistake when one person has both open.

    A Khmer string quoting the Chinese it teaches still has Khmer in it, so it
    is not caught; that is the case this phrasing exists to let through.
    """
    mine = SCRIPTS.get(locale)
    if mine is None or mine.search(text):
        return None
    for other, script in SCRIPTS.items():
        if other != locale and script.search(text):
            return other
    return None


def is_untranslated(text: str, english: str) -> bool:
    """Was this row left in English rather than translated?

    This replaced "the translation must contain Khmer or Han characters"
    (D-118). That rule had the right target and the wrong test: it rejected
    two correct translations — ``{text}`` for ``{text}``, and ``/ {limit}``
    for ``of {limit}``, which is how a Chinese status line writes 3/50 — while
    a string with nothing to translate can have no script in it by definition.

    What it was really guarding is a row copied across unchanged, so that is
    what is tested. Weaker in one direction, and knowingly: a translator who
    paraphrases in English rather than copying now gets through, where the
    script test would have caught them. The native-speaker review is the
    backstop for that; a rule that rejects correct work has no backstop.
    """
    if not has_translatable_words(english):
        return False
    return _without_parameters(text).casefold() == _without_parameters(english).casefold()


def usage_sites() -> dict[str, str]:
    """key -> where the code uses it, so a translator can tell a button from a sentence."""
    sites: dict[str, list[str]] = {}
    for path in i18n_check.python_files():
        for lineno, key in i18n_check.keys_used_in(path):
            sites.setdefault(key, []).append(f"{path.name}:{lineno}")
    return {key: ", ".join(sorted(set(where))) for key, where in sites.items()}


def pending(locale: str) -> list[str]:
    """Keys still carrying a placeholder, in catalogue order.

    A key whose English is empty on purpose is not pending and never will be:
    offering it is asking for a translation that would be wrong if given.
    """
    english = i18n.catalogue(i18n.SOURCE_LOCALE)
    catalogue = i18n.catalogue(locale)
    return [
        key
        for key, value in catalogue.items()
        if i18n.is_placeholder(value) and not i18n.is_deliberately_empty(english.get(key, ""))
    ]


def rows_for(locale: str, *, everything: bool = False) -> list[Row]:
    english = i18n.catalogue(i18n.SOURCE_LOCALE)
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


def write_csv(rows: Iterable[Row], path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("w", encoding=ENCODING, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
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
    english = i18n.catalogue(i18n.SOURCE_LOCALE)
    catalogue = i18n.catalogue(locale)

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
        if is_untranslated(text, english.get(key, "")):
            problems.append(
                Problem(key, "is the English unchanged; a copied row reads as finished")
            )
            continue
        other = wrong_language(text, locale=locale)
        if other is not None:
            problems.append(
                Problem(key, f"is written in {other}, not {locale}; was this the wrong sheet?")
            )
            continue
        good[key] = text

    return good, problems


def read_csv(path: Path) -> dict[str, str]:
    with path.open(encoding=ENCODING, newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or "key" not in reader.fieldnames:
            raise ValueError(f"{path} has no 'key' column; is it the exported sheet?")
        if "translation" not in reader.fieldnames:
            raise ValueError(f"{path} has no 'translation' column")
        return {
            (row.get("key") or "").strip(): (row.get("translation") or "")
            for row in reader
            if (row.get("key") or "").strip()
        }


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
        count = write_csv(rows, destination)
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
