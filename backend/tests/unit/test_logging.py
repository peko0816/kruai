"""Redaction is the mechanical enforcement of CODING_STANDARDS section 6.

"Never log secrets" held only by convention fails the first time someone logs a
whole provider payload. These tests pin which key names are considered
sensitive, so loosening the rule has to be deliberate.
"""

from __future__ import annotations

import json
import logging

import pytest
import structlog

from app.core.logging import REDACTED, configure_logging, get_logger, redact_processor


def redact(**event: object) -> dict[str, object]:
    return dict(redact_processor(None, "info", dict(event)))


@pytest.mark.parametrize(
    "key",
    [
        "token",
        "telegram_bot_token",
        "jwt_secret",
        "secret",
        "api_key",
        "apikey",
        "openai_api_key",
        "password",
        "authorization",
        "credential",
        "google_application_credentials",
        "phone",
        "phone_number",
        "audio",
        "audio_bytes",
        "raw",
        "raw_payload",
        "payload",
        "initdata",
        "init_data",
    ],
)
def test_sensitive_keys_are_redacted(key: str) -> None:
    assert redact(**{key: "leak-me"})[key] == REDACTED


@pytest.mark.parametrize("key", ["TOKEN", "JWT_Secret", "Phone"])
def test_redaction_is_case_insensitive(key: str) -> None:
    assert redact(**{key: "leak-me"})[key] == REDACTED


@pytest.mark.parametrize(
    "key",
    [
        "user_id",
        "concept_id",
        "pron_score",
        "provider",
        "latency_ms",
        "cost_usd_cents",
        "error_code",
        "audio_url",
        "duration_ms",
        "amount_minor",
        "currency",
    ],
)
def test_operational_fields_survive(key: str) -> None:
    """The three fields every external call must log must not be swallowed."""
    assert redact(**{key: "keep-me"})[key] == "keep-me"


def test_audio_url_survives_while_audio_does_not() -> None:
    """The URL is a pointer; the bytes are the content section 6 forbids."""
    out = redact(audio=b"\x00\x01", audio_url="https://cdn/x.mp3")
    assert out["audio"] == REDACTED
    assert out["audio_url"] == "https://cdn/x.mp3"


def test_nested_secrets_are_redacted() -> None:
    out = redact(context={"api_key": "sk-live", "provider": "azure"})
    assert out["context"] == {"api_key": REDACTED, "provider": "azure"}


def test_deeply_nested_secrets_are_redacted() -> None:
    out = redact(a={"b": {"c": {"jwt_secret": "sk-live"}}})
    assert out == {"a": {"b": {"c": {"jwt_secret": REDACTED}}}}


def test_self_referential_structure_terminates() -> None:
    """Depth cap: a cycle must not hang the logging call."""
    cycle: dict[str, object] = {"provider": "azure"}
    cycle["self"] = cycle

    assert redact(context=cycle)["context"] is not None


def test_event_name_is_untouched() -> None:
    assert redact(event="attempt.scored", token="x")["event"] == "attempt.scored"


# ------------------------------------------------------------------- pipeline


def test_json_output_is_parseable_and_redacted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(log_level="INFO", json_output=True)
    try:
        get_logger("test").info(
            "attempt.scored", user_id="u-1", pron_score=72.5, jwt_secret="sk-live"
        )
        line = capsys.readouterr().err.strip().splitlines()[-1]
        record = json.loads(line)

        assert record["event"] == "attempt.scored"
        assert record["user_id"] == "u-1"
        assert record["pron_score"] == 72.5
        assert record["jwt_secret"] == REDACTED
        assert record["level"] == "info"
        assert "timestamp" in record
    finally:
        structlog.reset_defaults()
        logging.getLogger().handlers = []


def test_log_level_is_honoured(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(log_level="WARNING", json_output=True)
    try:
        log = get_logger("test")
        log.info("suppressed.event")
        log.warning("emitted.event")

        err = capsys.readouterr().err
        assert "suppressed.event" not in err
        assert "emitted.event" in err
    finally:
        structlog.reset_defaults()
        logging.getLogger().handlers = []


# ------------------------------------------------------------ tokens in text

#: Shaped like a real bot token: a numeric id, a colon, 35 URL-safe characters.
BOT_TOKEN = "7412345678:AAHk3_x9QpZrT0vWmY2bLcD4eFgHiJkLmNo"


def _json_lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, object]]:
    return [json.loads(line) for line in capsys.readouterr().err.strip().splitlines()]


def test_a_bot_token_inside_a_library_message_is_scrubbed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """python-telegram-bot logs the request URL, which carries the token (D-144)."""
    configure_logging(log_level="INFO", json_output=True)
    try:
        logging.getLogger("telegram.ext").error(
            "POST https://api.telegram.org/bot%s/getUpdates failed", BOT_TOKEN
        )
        record = _json_lines(capsys)[-1]

        assert BOT_TOKEN not in json.dumps(record)
        assert record["event"] == f"POST https://api.telegram.org/bot{REDACTED}/getUpdates failed"
    finally:
        structlog.reset_defaults()
        logging.getLogger().handlers = []


def test_a_bot_token_is_scrubbed_from_development_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(log_level="INFO", json_output=False)
    try:
        logging.getLogger("telegram.ext").error("GET .../bot%s/getMe", BOT_TOKEN)
        err = capsys.readouterr().err

        assert BOT_TOKEN not in err
        assert REDACTED in err
    finally:
        structlog.reset_defaults()
        logging.getLogger().handlers = []


def test_a_bot_token_quoted_in_a_traceback_is_scrubbed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(log_level="INFO", json_output=True)
    try:
        try:
            raise ValueError(f"The token `{BOT_TOKEN}` was rejected by the server.")
        except ValueError:
            get_logger("test").exception("bot.failed")
        record = _json_lines(capsys)[-1]

        assert BOT_TOKEN not in json.dumps(record)
        assert REDACTED in str(record["exception"])
    finally:
        structlog.reset_defaults()
        logging.getLogger().handlers = []


def test_request_lines_are_kept_out_at_info(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(log_level="INFO", json_output=True)
    try:
        logging.getLogger("httpx").info("HTTP Request: POST https://api.telegram.org/x")
        logging.getLogger("httpcore").info("connect_tcp.started")
        logging.getLogger("httpx").warning("httpx.warned")
        err = capsys.readouterr().err

        assert "HTTP Request" not in err
        assert "connect_tcp" not in err
        assert "httpx.warned" in err
    finally:
        structlog.reset_defaults()
        logging.getLogger().handlers = []


@pytest.mark.parametrize(
    "text",
    [
        "lesson d754344b-d6c4-474e-a38b-cdda06e53a02 step 1",
        "2026-11-06T08:59:53.946125Z",
        "telegram_id=990001 score 72:5",
        "http://media/zh/77/7772b3d2f3a52eb407a58.mp3",
        "error:scoring_unsupported_audio_format_refused",
        "lesson 10:30 attempt_scored_without_reference_text",
    ],
    ids=["uuid", "timestamp", "ids", "media-url", "code", "time-then-word"],
)
def test_ordinary_text_is_not_mistaken_for_a_token(text: str) -> None:
    from app.core.logging import scrub_processor

    assert scrub_processor(None, "info", {"event": text})["event"] == text
