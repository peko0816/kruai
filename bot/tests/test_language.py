"""Which language the bot speaks before it knows the account's (Q12, D-148)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot import i18n
from bot.handlers import LANGUAGE_COMMANDS, learner_from


@pytest.mark.parametrize(
    ("language_code", "expected"),
    [("en", "km"), ("en-US", "km"), ("km", "km"), ("zh-hans", "zh"), ("zh", "zh"), (None, "km")],
)
def test_the_first_guess_is_khmer_unless_telegram_is_chinese(
    language_code: str | None, expected: str
) -> None:
    """Telegram has no Khmer interface; Cambodians mostly run it in English."""
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1, language_code=language_code))

    assert learner_from(update).locale == expected  # type: ignore[arg-type]


@pytest.mark.parametrize("locale", sorted(i18n.SUPPORTED_LOCALES))
def test_every_menu_offers_every_language(locale: str) -> None:
    menu = i18n.t("bot.language_menu", locale=locale)

    for command in LANGUAGE_COMMANDS:
        assert f"/{command}" in menu


def test_the_commands_cover_the_shipped_languages() -> None:
    assert sorted(LANGUAGE_COMMANDS.values()) == sorted(i18n.SUPPORTED_LOCALES)
