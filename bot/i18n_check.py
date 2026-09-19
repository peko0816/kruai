"""Checks the message catalogue against the code that uses it.

BACKLOG D7. Three things fail a build, one is reported and does not:

  1. the three locales carry the same keys — a key in one file and not another
     is a blank line on somebody's screen, in one language only;
  2. every key the code references exists — a typo in a key is a
     MissingMessageError in front of a learner, and it is findable here;
  3. nothing user-visible is written in Python (CLAUDE.md section 7);
  4. how many translations are still placeholders. **Reported, not failed.**

Point 4 is the one worth explaining. "km must have no placeholders" is a launch
gate, not a commit gate: every key is a placeholder today, so failing on it
would hold the build red through the rest of the milestone and the check would
be switched off within a week. So the count is printed on every run, where it
can only go down, and ``--strict`` — the form the M2 checklist runs — is what
refuses.

**Keys are recognised by position, never by shape.** ``bot.turn`` is a log
event and ``attempt.audio_too_large`` is an error code; both look exactly like
message keys and neither is one (CODING_STANDARDS sections 6 and 5.1). So this
reads the places that are message keys by construction:

  · the first argument of ``Reply(...)`` and of ``i18n.t(...)``;
  · anything assigned to a name ending ``_KEY`` or ``_KEYS``.

That last one is a convention this file makes enforceable: name a constant for
what it holds and the checker finds it.
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from bot import i18n

_ROOT: Final = Path(__file__).resolve().parents[1]

#: Where code that references message keys lives. The backend emits keys it
#: never renders — the API hands a client ``feedback.key`` and the avatar
#: disclaimer — so a typo there is just as invisible and is checked here too.
SOURCE_ROOTS: Final[tuple[Path, ...]] = (_ROOT / "bot", _ROOT / "backend" / "app")

#: Calls whose first argument is a message key.
_KEY_CALLS: Final[frozenset[str]] = frozenset({"Reply", "t"})

#: Suffixes that declare a constant holds message keys.
_KEY_NAME_SUFFIXES: Final[tuple[str, ...]] = ("_KEY", "_KEYS")

#: Places in the bot that put text in front of a learner. A literal in one of
#: these is copy written in Python, whatever it says.
_SEND_CALLS: Final[frozenset[str]] = frozenset({"reply_text", "reply_voice", "reply_audio"})
_SEND_KWARGS: Final[frozenset[str]] = frozenset({"text", "caption"})

#: The checker's own tests build deliberately broken samples; excluding tests
#: keeps those from being read as violations.
_EXCLUDED_PARTS: Final[frozenset[str]] = frozenset({"tests"})


@dataclass
class Findings:
    """Everything one run learned."""

    #: locale -> keys it is missing relative to the reference catalogue.
    missing_by_locale: dict[str, set[str]] = field(default_factory=dict)
    #: locale -> keys it has that the reference does not.
    extra_by_locale: dict[str, set[str]] = field(default_factory=dict)
    #: "path:line key" for keys used in code that no catalogue defines.
    unknown_keys: list[str] = field(default_factory=list)
    #: "path:line" for user-visible strings written in Python.
    inline_strings: list[str] = field(default_factory=list)
    #: locale -> keys still awaiting a translator.
    placeholders_by_locale: dict[str, set[str]] = field(default_factory=dict)
    #: Keys nobody references. Reported: a key a client will need later is not
    #: a defect, but a key nobody ever uses is translation work wasted.
    unreferenced_keys: set[str] = field(default_factory=set)

    @property
    def blocking(self) -> list[str]:
        """Problems that fail a build, as readable lines."""
        problems: list[str] = []
        for locale, keys in sorted(self.missing_by_locale.items()):
            if keys:
                problems.append(f"{locale}.json is missing {len(keys)} key(s): {sorted(keys)}")
        for locale, keys in sorted(self.extra_by_locale.items()):
            if keys:
                problems.append(f"{locale}.json defines {len(keys)} unknown key(s): {sorted(keys)}")
        problems.extend(f"unknown message key at {where}" for where in self.unknown_keys)
        problems.extend(
            f"user-visible string written in Python at {where}" for where in self.inline_strings
        )
        return problems

    @property
    def placeholder_total(self) -> int:
        return sum(len(keys) for keys in self.placeholders_by_locale.values())


def python_files(roots: tuple[Path, ...] = SOURCE_ROOTS) -> Iterator[Path]:
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            if _EXCLUDED_PARTS.isdisjoint(path.parts):
                yield path


def keys_used_in(path: Path) -> list[tuple[int, str]]:
    """Message keys referenced by one file, with line numbers."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_name(node.func) in _KEY_CALLS:
            if node.args and isinstance(node.args[0], ast.Constant):
                found.extend(_literal_keys(node.args[0]))
            for keyword in node.keywords:
                if keyword.arg == "key":
                    found.extend(_literal_keys(keyword.value))
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            if _declares_keys(node):
                found.extend(_literal_keys(node.value))

    return found


def inline_strings_in(path: Path) -> list[int]:
    """Lines where a literal is handed to something that shows it to a learner."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[int] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        if name in _SEND_CALLS:
            found.extend(arg.lineno for arg in node.args if _is_text_literal(arg))
        if name in _SEND_CALLS or name == "Outgoing":
            found.extend(
                keyword.value.lineno
                for keyword in node.keywords
                if keyword.arg in _SEND_KWARGS and _is_text_literal(keyword.value)
            )
    return found


def run(
    *,
    roots: tuple[Path, ...] = SOURCE_ROOTS,
    catalogues: dict[str, dict[str, str]] | None = None,
    base: Path = _ROOT,
) -> Findings:
    """Check the catalogue against the code and report everything found.

    The arguments exist for one reason: a check that has only ever been run
    against a clean repository has not been shown to detect anything. Every
    assertion about it would be ``== []``, which passes just as well when the
    scanner is blind — which is exactly what four mutations demonstrated before
    these existed. The tests run it over a directory built to be broken.
    """
    findings = Findings()
    entries_by_locale = catalogues or {
        locale: i18n.catalogue(locale) for locale in i18n.SUPPORTED_LOCALES
    }
    default = (
        i18n.DEFAULT_LOCALE
        if i18n.DEFAULT_LOCALE in entries_by_locale
        else next(iter(entries_by_locale))
    )
    reference = set(entries_by_locale[default])

    for locale, entries in entries_by_locale.items():
        findings.missing_by_locale[locale] = reference - set(entries)
        findings.extra_by_locale[locale] = set(entries) - reference
        findings.placeholders_by_locale[locale] = {
            key for key, value in entries.items() if i18n.is_placeholder(value)
        }

    referenced: set[str] = set()
    for path in python_files(roots):
        relative = path.relative_to(base)
        for lineno, key in keys_used_in(path):
            referenced.add(key)
            if key not in reference:
                findings.unknown_keys.append(f"{relative}:{lineno} {key!r}")
        findings.inline_strings.extend(f"{relative}:{lineno}" for lineno in inline_strings_in(path))

    findings.unreferenced_keys = reference - referenced
    return findings


def report(findings: Findings, *, strict: bool) -> int:
    """Print what was found. Non-zero means do not ship this."""
    for problem in findings.blocking:
        print(f"error: {problem}", file=sys.stderr)

    for locale in sorted(findings.placeholders_by_locale, key=i18n.SUPPORTED_LOCALES.index):
        pending = findings.placeholders_by_locale.get(locale, set())
        total = len(i18n.catalogue(locale))
        state = "complete" if not pending else f"{len(pending)}/{total} awaiting a translator"
        print(f"{locale}: {state}")

    if findings.unreferenced_keys:
        print(
            f"note: {len(findings.unreferenced_keys)} key(s) nobody references yet: "
            f"{sorted(findings.unreferenced_keys)}"
        )

    if findings.blocking:
        return 1
    if strict and findings.placeholder_total:
        print(
            "error: translations are incomplete; this is the launch gate, "
            "not the commit gate (docs/DEFINITION_OF_DONE.md, M2)",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--strict",
        action="store_true",
        help="also fail while any translation is still a placeholder (the launch gate)",
    )
    arguments = parser.parse_args(argv)
    return report(run(), strict=arguments.strict)


# ------------------------------------------------------------------ internals


def _call_name(func: ast.expr) -> str:
    """The bare name of what is being called: ``i18n.t`` reads as ``t``."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _declares_keys(node: ast.Assign | ast.AnnAssign) -> bool:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return any(
        isinstance(target, ast.Name) and target.id.endswith(_KEY_NAME_SUFFIXES)
        for target in targets
    )


def _literal_keys(node: ast.expr | None) -> list[tuple[int, str]]:
    """Every string literal in a value, whether it is one or a collection."""
    if node is None:
        return []
    if isinstance(node, ast.Constant):
        return [(node.lineno, node.value)] if isinstance(node.value, str) else []
    if isinstance(node, ast.Dict):
        return [pair for value in node.values for pair in _literal_keys(value)]
    if isinstance(node, ast.List | ast.Tuple | ast.Set):
        return [pair for element in node.elts for pair in _literal_keys(element)]
    if isinstance(node, ast.Call):
        # frozenset({...}), tuple([...]) and friends.
        return [pair for argument in node.args for pair in _literal_keys(argument)]
    return []


def _is_text_literal(node: ast.expr) -> bool:
    """A string a human would read, as opposed to a URL or an empty string."""
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and bool(node.value)


if __name__ == "__main__":
    raise SystemExit(main())
