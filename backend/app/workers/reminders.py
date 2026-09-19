"""Putting a renewal reminder in front of a learner.

The manual path exists because most Cambodian channels cannot take money
without the learner acting (ARCHITECTURE 3.4), so the reminder *is* the renewal
mechanism — if it does not arrive, the subscription simply stops.

Which is why this reports whether it was delivered, and why the job only writes
``reminder_sent_at`` when it was. A reminder recorded as sent and never sent is
the one failure that leaves a paying learner with no warning at all.

The text comes from locales/ like everything else a learner reads (CLAUDE.md
section 7). This module renders a key; it does not write a sentence.
"""

from __future__ import annotations

from bot import i18n
from telegram import Bot
from telegram.error import TelegramError

from app.core.config import Settings
from app.core.logging import get_logger

log = get_logger(__name__)


class TelegramReminder:
    """Sends through the same bot the learner already talks to."""

    def __init__(self, settings: Settings) -> None:
        token = settings.telegram_bot_token.get_secret_value()
        if not token.strip():
            raise ValueError(
                "TELEGRAM_BOT_TOKEN is empty; the reminder job cannot deliver "
                "anything, and marking reminders sent without sending them "
                "would leave paying learners with no warning at all"
            )
        self._bot = Bot(token)

    async def send(self, telegram_id: int, *, message_key: str, locale: str) -> bool:
        """True when Telegram accepted the message.

        A learner who has blocked the bot, or deleted their account, raises
        here — and that is a delivery failure like any other, not a crash: the
        job carries on with the rest and reports the number.
        """
        try:
            await self._bot.send_message(
                chat_id=telegram_id, text=i18n.t(message_key, locale=locale)
            )
        except TelegramError as exc:
            log.warning(
                "reminders.undelivered",
                telegram_id=telegram_id,
                error=type(exc).__name__,
            )
            return False
        return True


__all__ = ["TelegramReminder"]
