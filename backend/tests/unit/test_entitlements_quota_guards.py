"""Argument guards that run before any database work.

``Entitlements`` is otherwise integration-tested, because its whole point is
that the check and the write are one statement. These guards are the part that
needs no database — and the assertion worth making is not only that they raise,
but that they raise *without opening a connection*. A rejected argument that
still checks out a connection and starts a transaction is a slow leak under a
caller passing junk.

The bool case is the one that would not announce itself. mypy sees ``True`` as a
valid ``int`` because it is one, so ``count=True`` type-checks and would consume
exactly one attempt from a caller that meant something else entirely.
"""

from __future__ import annotations

import uuid
from typing import Any, NoReturn

import pytest

from app.core.config import Settings
from app.services.entitlements import Entitlements

_MINIMAL: dict[str, str] = {
    "DATABASE_URL": "postgresql+psycopg://u:p@localhost:5432/db",
    "REDIS_URL": "redis://localhost:6379/0",
    "TELEGRAM_BOT_TOKEN": "",
    "AZURE_SPEECH_KEY": "",
    "AZURE_SPEECH_REGION": "",
    "GOOGLE_APPLICATION_CREDENTIALS": "",
    "ELEVENLABS_API_KEY": "",
    "OPENAI_API_KEY": "",
    "PAYWAY_MERCHANT_ID": "",
    "PAYWAY_API_KEY": "",
    "PAYWAY_BASE_URL": "",
    "BAKONG_TOKEN": "",
    "JWT_SECRET": "",
}

USER = uuid.UUID("3f2504e0-4f89-11d3-9a0c-0305e82c3301")


def unusable_session() -> NoReturn:
    """A session factory that fails the test if anything reaches the database."""
    raise AssertionError("a rejected argument must not open a database session")


@pytest.fixture
def quota(monkeypatch: pytest.MonkeyPatch) -> Entitlements:
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    values: dict[str, Any] = dict(_MINIMAL)
    return Entitlements(
        # No ignore needed: NoReturn is the bottom type, so a factory that
        # only ever raises satisfies any return type mypy asks for.
        session_factory=unusable_session,
        settings=Settings(_env_file=None, **values),
    )


@pytest.mark.parametrize("count", [True, False])
async def test_a_bool_count_is_refused(quota: Entitlements, count: bool) -> None:
    """``True`` is an int of value 1, so without this guard it would quietly
    consume one attempt and type-check while doing it."""
    with pytest.raises(TypeError, match="count must be int, got bool"):
        await quota.consume_attempt(USER, plan="free", count=count)


@pytest.mark.parametrize("seconds", [1.5, "30", None])
async def test_a_non_integer_number_of_seconds_is_refused(
    quota: Entitlements, seconds: object
) -> None:
    """Realtime is metered in whole seconds (CODING_STANDARDS section 3); a float
    would round somewhere unstated and a string would compare as greater than
    every integer in the guard below it."""
    with pytest.raises(TypeError, match="seconds must be int"):
        await quota.consume_realtime_seconds(USER, seconds=seconds)  # type: ignore[arg-type]


async def test_a_bool_grant_is_refused(quota: Entitlements) -> None:
    with pytest.raises(TypeError, match="seconds must be int, got bool"):
        await quota.grant_realtime_seconds(USER, seconds=True)


@pytest.mark.parametrize("count", [0, -1])
async def test_a_non_positive_count_is_refused(quota: Entitlements, count: int) -> None:
    """Zero is a no-op that still reports success; a negative one is a refund
    through the deduction path."""
    with pytest.raises(ValueError, match="count must be positive"):
        await quota.consume_task(USER, plan="free", count=count)
