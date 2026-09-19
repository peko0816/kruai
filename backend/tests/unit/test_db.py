"""The connection pool is the one the configuration asked for (D-073).

Nothing about a wrongly-sized pool shows up in a test that exercises behaviour:
the endpoints answer correctly whether the pool holds five connections or fifty,
they just answer fewer at a time. So the wiring is asserted directly, because
the alternative is a deployment that silently runs on a library default nobody
chose.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.config import Settings
from app.core.db import create_engine

_MINIMAL: dict[str, Any] = {
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


def settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **{**_MINIMAL, **overrides})


def test_the_pool_is_sized_by_configuration() -> None:
    engine = create_engine(
        settings(DB_POOL_SIZE="7", DB_MAX_OVERFLOW="3", DB_POOL_TIMEOUT_SECONDS="4")
    )

    pool: Any = engine.pool
    assert pool.size() == 7
    assert pool._max_overflow == 3
    assert pool._timeout == 4


def test_the_shipped_pool_is_not_the_library_default() -> None:
    """SQLAlchemy's own defaults are 5 and 10, and waiting half a minute for a
    connection is not something a learner uploading a voice note should do."""
    engine = create_engine(settings())

    pool: Any = engine.pool
    assert pool.size() == 20
    assert pool._timeout == 10


@pytest.mark.parametrize("value", ["0", "-1"])
def test_a_pool_of_nothing_refuses_to_load(value: str) -> None:
    with pytest.raises(ValueError):
        settings(DB_POOL_SIZE=value)
