"""structlog configuration.

Events are structured, never interpolated strings (CODING_STANDARDS section 6)::

    log.info("attempt.scored", user_id=uid, provider=scorer.name,
             pron_score=r.pron_score, cost_usd_cents=r.cost_usd_cents)

Dev renders to a readable console; staging and prod emit JSON so the log sink
can index the fields. Library logs (uvicorn, SQLAlchemy) are routed through the
same renderer so a production log stream is uniformly parseable.

The redaction processor makes "never log secrets" a property of the pipeline
rather than a rule people have to remember at each call site. It is the last
line of defence, not permission to pass secrets to the logger.
"""

from __future__ import annotations

import logging
import sys
from typing import Final

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

REDACTED: Final = "[redacted]"

#: A key containing any of these is a credential by naming convention.
_SENSITIVE_SUBSTRINGS: Final[tuple[str, ...]] = (
    "token",
    "secret",
    "password",
    "api_key",
    "apikey",
    "credential",
    "authorization",
    "phone",
)

#: Exact key names that carry bulk or raw provider data. Matched exactly so
#: that descriptive siblings survive: ``audio`` is redacted, ``audio_url`` and
#: ``duration_ms`` are kept, since the URL is not the content.
_SENSITIVE_EXACT: Final[frozenset[str]] = frozenset(
    {"audio", "audio_bytes", "raw", "raw_payload", "payload", "initdata", "init_data"}
)

#: Depth cap so a self-referential structure cannot hang the logger.
_MAX_REDACT_DEPTH: Final = 4


def is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in _SENSITIVE_EXACT:
        return True
    return any(marker in lowered for marker in _SENSITIVE_SUBSTRINGS)


def _redact_value(value: object, depth: int) -> object:
    if depth >= _MAX_REDACT_DEPTH or not isinstance(value, dict):
        return value
    return {
        key: REDACTED if is_sensitive_key(str(key)) else _redact_value(item, depth + 1)
        for key, item in value.items()
    }


def redact_processor(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
    """Replace values whose key names them as sensitive, nested dicts included."""
    for key in list(event_dict):
        if is_sensitive_key(str(key)):
            event_dict[key] = REDACTED
        else:
            event_dict[key] = _redact_value(event_dict[key], 0)
    return event_dict


def _shared_processors() -> list[Processor]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        redact_processor,
        structlog.processors.StackInfoRenderer(),
    ]


def configure_logging(*, log_level: str = "INFO", json_output: bool = False) -> None:
    """Install the logging pipeline. Idempotent — safe to call again.

    Args:
        log_level: One of the stdlib level names.
        json_output: True for staging/prod. Callers normally pass
            ``settings.env != "dev"``.
    """
    shared = _shared_processors()

    renderer: Processor
    if json_output:
        # ConsoleRenderer formats exceptions itself; format_exc_info would
        # consume exc_info first and leave it with nothing to render.
        render_chain: list[Processor] = [structlog.processors.format_exc_info]
        renderer = structlog.processors.JSONRenderer()
    else:
        render_chain = []
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        # foreign_pre_chain handles records from stdlib loggers, which never
        # passed through structlog's own processor chain.
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            *render_chain,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(log_level)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound logger. Use the module's ``__name__``."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
