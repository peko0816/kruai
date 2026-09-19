"""Message catalogue. No user-visible string is ever written in Python.

CLAUDE.md section 7 and CODING_STANDARDS section 11. The rule is not about
tidiness: Khmer is the teaching language (PRD 1.1) and it has to be written by a
native speaker, which cannot happen while the sentences live inside handler
code.

A missing translation renders as its placeholder — ``[[km:bot.welcome]]`` —
rather than falling back to English. A silent fallback ships an English bot to
Khmer learners and nothing in the logs says so; a placeholder is ugly on screen
and impossible to miss in review. BACKLOG D7 adds the CI check that km carries
none of them before launch.

This is the loader, not the framework D7 will build: it reads the three files,
looks a key up, and fills parameters. What D7 adds is the checking.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Final

#: Repository root: bot/i18n.py -> bot -> <root>
_ROOT: Final = Path(__file__).resolve().parents[1]
LOCALES_DIR: Final = _ROOT / "locales"

#: The three locales the product ships (CODING_STANDARDS section 11). Khmer
#: first because it is the teaching language, not an afterthought.
SUPPORTED_LOCALES: Final[tuple[str, ...]] = ("km", "zh", "en")

#: What a learner falls back to when their locale is not one we ship.
DEFAULT_LOCALE: Final = "km"


class MissingMessageError(KeyError):
    """A key no catalogue defines.

    Raised rather than rendered as the key itself. A key on screen looks like a
    translation nobody has written yet, which is a different problem with a
    different fix — this one is a typo in code, and it should fail where it can
    be seen.
    """


@cache
def catalogue(locale: str) -> dict[str, str]:
    """One locale's strings, read once per process.

    Raises:
        FileNotFoundError: no such locale file. Locales are a fixed set, so
            this is a deployment missing a file rather than a learner setting.
    """
    return dict(
        json.loads((LOCALES_DIR / f"{locale}.json").read_text(encoding="utf-8")),
    )


def normalise_locale(locale: str | None) -> str:
    """The shipped locale closest to what the caller asked for."""
    if locale and locale in SUPPORTED_LOCALES:
        return locale
    return DEFAULT_LOCALE


def is_placeholder(value: str) -> bool:
    """True for an untranslated entry, e.g. ``[[km:bot.welcome]]``."""
    return value.startswith("[[") and value.endswith("]]")


def t(key: str, *, locale: str = DEFAULT_LOCALE, **params: object) -> str:
    """Render one message.

    Parameters are substituted with ``str.format``, so a placeholder entry —
    which has no braces in it — renders as itself rather than failing. That is
    deliberate: an untranslated string should still get through the pipeline
    and be visible on screen, not crash the handler that used it.

    Raises:
        MissingMessageError: the key is in no catalogue.
        KeyError: the message names a parameter the caller did not supply,
            which is a bug in the call site rather than in the translation.
    """
    resolved = normalise_locale(locale)
    template = catalogue(resolved).get(key)
    if template is None:
        # Fall back to the default locale's catalogue before giving up: a key
        # added to km but not yet to zh is a translation gap, not a typo.
        template = catalogue(DEFAULT_LOCALE).get(key)
    if template is None:
        raise MissingMessageError(
            f"no message {key!r} in any catalogue; keys live in {LOCALES_DIR}"
        )
    return template.format(**params) if params else template


__all__ = [
    "DEFAULT_LOCALE",
    "LOCALES_DIR",
    "SUPPORTED_LOCALES",
    "MissingMessageError",
    "catalogue",
    "is_placeholder",
    "normalise_locale",
    "t",
]
