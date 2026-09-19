"""The message catalogue, and the rule that nothing is inlined.

CLAUDE.md section 7. The tests that matter are the structural ones: three files
with the same keys, and no Khmer string quietly falling back to English.
"""

from __future__ import annotations

import json

import pytest

from bot import i18n


def test_every_locale_defines_the_same_keys() -> None:
    """A key added to one file and not the others is a blank screen later."""
    catalogues = {locale: i18n.catalogue(locale) for locale in i18n.SUPPORTED_LOCALES}
    reference = set(catalogues["en"])

    for locale, entries in catalogues.items():
        assert set(entries) == reference, f"{locale}.json has drifted from en.json"


def test_khmer_is_the_default_locale() -> None:
    """Khmer is the teaching language (PRD 1.1), not a translation of English."""
    assert i18n.DEFAULT_LOCALE == "km"


@pytest.mark.parametrize(
    ("asked", "expected"),
    [("km", "km"), ("zh", "zh"), ("en", "en"), ("ru", "km"), ("", "km"), (None, "km")],
)
def test_an_unshipped_locale_falls_back_to_khmer(asked: str | None, expected: str) -> None:
    assert i18n.normalise_locale(asked) == expected


def test_a_missing_translation_renders_as_a_placeholder_not_as_english() -> None:
    """A silent fallback ships an English bot to Khmer learners and says
    nothing in the logs. A placeholder is impossible to miss."""
    rendered = i18n.t("bot.welcome", locale="km")

    assert i18n.is_placeholder(rendered)
    assert rendered == "[[km:bot.welcome]]"
    assert "KruAI" not in rendered


def test_parameters_are_substituted() -> None:
    assert i18n.t("bot.status_limit_suffix", locale="en", limit=10) == " of 10"


def test_a_placeholder_survives_parameter_substitution() -> None:
    """An untranslated string still has to reach the screen; it has no braces
    to fill, and failing here would take down the handler that used it."""
    assert i18n.t("bot.lesson_started", locale="km", title="x", count=3).startswith("[[km:")


def test_an_unknown_key_is_an_error_rather_than_a_key_on_screen() -> None:
    """A key on screen looks like a missing translation, which is somebody
    else's fix. This one is a typo in code."""
    with pytest.raises(i18n.MissingMessageError):
        i18n.t("bot.does_not_exist", locale="en")


def test_the_english_catalogue_carries_no_placeholders() -> None:
    """en is the one locale with real sentences today, so the bot works in
    development. km and zh are D7's, with a native speaker."""
    entries = i18n.catalogue("en")

    assert [key for key, value in entries.items() if i18n.is_placeholder(value)] == []


@pytest.mark.parametrize("locale", ["km", "zh"])
def test_the_untranslated_catalogues_are_entirely_placeholders(locale: str) -> None:
    """Half-translated would be worse than untranslated: it reads as finished."""
    entries = i18n.catalogue(locale)

    assert all(i18n.is_placeholder(value) for value in entries.values())


def test_the_catalogues_are_valid_json_on_disk() -> None:
    for locale in i18n.SUPPORTED_LOCALES:
        path = i18n.LOCALES_DIR / f"{locale}.json"
        assert isinstance(json.loads(path.read_text(encoding="utf-8")), dict)
