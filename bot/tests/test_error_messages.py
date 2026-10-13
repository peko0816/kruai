"""What a learner is told when the API refuses (bot/handlers.py).

The mapping from an error code to a message key is the only place where an
internal code becomes a sentence. Nothing else in the suite looks at it, and a
mutation that deleted one of its rows went unnoticed: every test still passed
while learners were shown the generic failure instead of the thing they could
act on.
"""

from __future__ import annotations

import pytest

from bot import i18n
from bot.api_client import ApiError
from bot.handlers import _ERROR_MESSAGE_KEYS, Conversation, Learner


@pytest.fixture
def learner() -> Learner:
    return Learner(telegram_id=42, locale="en")


def message_for(code: str, status: int, learner: Learner) -> str:
    conversation = Conversation.__new__(Conversation)  # no API, no Redis: only the mapping
    conversation._api = None  # type: ignore[assignment]  # unused unless status is 401
    return conversation._on_api_error(ApiError(status=status, code=code), learner)[0].text


@pytest.mark.parametrize(("code", "key"), sorted(_ERROR_MESSAGE_KEYS.items()))
def test_every_mapped_code_renders_its_own_message(code: str, key: str, learner: Learner) -> None:
    """Each row of the table has to reach the catalogue, not the fallback."""
    assert message_for(code, 402, learner) == i18n.t(
        key, locale="en", **({"resets_at": ""} if key == "bot.quota_exhausted" else {})
    )


def test_a_learner_told_there_is_nothing_to_buy_is_not_told_to_buy(learner: Learner) -> None:
    """D-100: the one refusal where "upgrade" is not an action the learner has."""
    assert "payment.unavailable" in _ERROR_MESSAGE_KEYS

    said = message_for("payment.unavailable", 503, learner)

    assert said == i18n.t("bot.payments_unavailable", locale="en")
    assert said != i18n.t("bot.error", locale="en")


def test_an_unmapped_code_falls_back_rather_than_leaking_it(learner: Learner) -> None:
    """A learner should never be shown an internal code."""
    said = message_for("some.internal.code", 500, learner)

    assert said == i18n.t("bot.error", locale="en")
    assert "some.internal.code" not in said
