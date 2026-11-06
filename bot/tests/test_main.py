"""How the bot process ends (bot/main.py, D-144): through the log pipeline,
which scrubs tokens, never through the interpreter's raw traceback."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
import structlog
from telegram.error import InvalidToken

from app.core.logging import configure_logging
from bot.main import run

BOT_TOKEN = "7412345678:AAHk3_x9QpZrT0vWmY2bLcD4eFgHiJkLmNo"


@contextmanager
def json_logs() -> Iterator[None]:
    """Configured inside the test, after pytest has replaced stderr."""
    configure_logging(log_level="INFO", json_output=True)
    try:
        yield
    finally:
        structlog.reset_defaults()
        logging.getLogger().handlers = []


def _raise(error: Exception) -> object:
    raise error


def test_a_rejected_token_is_one_clear_line_without_the_token(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with json_logs():
        code = run(
            lambda: _raise(InvalidToken(f"The token `{BOT_TOKEN}` was rejected by the server."))
        )

    err = capsys.readouterr().err
    record = json.loads(err.strip().splitlines()[-1])
    assert code == 1
    assert record["event"] == "bot.token_rejected"
    assert BOT_TOKEN not in err
    assert "Traceback" not in err


def test_any_other_crash_is_logged_scrubbed_and_exits_non_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with json_logs():
        code = run(
            lambda: _raise(RuntimeError(f"failed at https://api.telegram.org/bot{BOT_TOKEN}/getMe"))
        )

    err = capsys.readouterr().err
    record = json.loads(err.strip().splitlines()[-1])
    assert code == 1
    assert record["event"] == "bot.crashed"
    assert "RuntimeError" in str(record["exception"])
    assert BOT_TOKEN not in err


def test_a_clean_stop_exits_zero() -> None:
    assert run(lambda: None) == 0
