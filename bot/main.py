"""Run the bot.

Polling, not webhooks. PRD section 8 specifies webhook mode and that is right
for production — it needs a public HTTPS endpoint, which this project does not
have yet. Polling is one call away from the same thing (``run_webhook`` takes
the URL and a secret token) and it is confined to this file, so switching is a
deployment change rather than a code change anywhere else (docs/DECISIONS.md
D-049).

    make bot

Everything it needs is in .env: TELEGRAM_BOT_TOKEN to talk to Telegram and to
authenticate against the API, BOT_API_BASE_URL to find the API, REDIS_URL to
remember where each learner is.
"""

from __future__ import annotations

import httpx
from redis.asyncio import Redis
from telegram.ext import Application

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from bot.api_client import KruaiApi
from bot.handlers import Conversation, register
from bot.session import SessionStore

log = get_logger(__name__)


def build_application(settings: Settings) -> Application:  # type: ignore[type-arg]
    """Wire the bot against a configuration. Separated so it can be inspected."""
    token = settings.telegram_bot_token.get_secret_value()
    if not token.strip():
        raise ValueError(
            "TELEGRAM_BOT_TOKEN is empty; the bot cannot talk to Telegram and "
            "cannot authenticate against the API without it"
        )

    api = KruaiApi(
        httpx.AsyncClient(base_url=settings.bot_api_base_url, timeout=30.0),
        bot_token=token,
    )
    sessions = SessionStore(
        Redis.from_url(settings.redis_url),
        ttl_seconds=settings.bot_session_ttl_seconds,
    )
    conversation = Conversation(api, sessions, max_retry=settings.scoring_max_retry)

    application = Application.builder().token(token).build()
    register(application, conversation)
    return application


def main() -> None:
    settings = get_settings()
    configure_logging(log_level=settings.log_level, json_output=settings.env != "dev")
    log.info("bot.starting", api=settings.bot_api_base_url, env=settings.env)
    build_application(settings).run_polling()


if __name__ == "__main__":
    main()
