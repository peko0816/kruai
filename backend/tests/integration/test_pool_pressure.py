"""Every endpoint, with more requests in flight than the pool has connections.

One shape of bug, found by probing during the G-D gate and worth a file of its
own because it is invisible below its own threshold.

A request holds a database connection from its first query until it commits.
Several services open a session of their *own* on purpose -- the allowance
deduction, the daily reset, the cost ledger -- because their writes have to
survive whatever the request's transaction does. Put those together and a
request holds one connection while asking for a second. Once that many requests
are in flight, every one of them holds a connection and every one of them waits
for a connection only another can give back. They sit there for the whole pool
timeout and then fail: slow first, then a 500.

**The pool here is deliberately tiny.** Two connections, no overflow, and a
two-second patience. Against the shipped pool of thirty this would need thirty
one simultaneous requests and would still only catch the cliff when the timing
happened to line up; against a pool of two, a single endpoint that wants two
connections at once stops answering every time. Small and deterministic beats
large and probabilistic.

Every request comes from a different learner. One learner firing the same
request repeatedly is a weaker test than it looks: the idempotent endpoints
short-circuit after the first, so only that first one ever reaches the code
that reaches for a second connection.

Anything added to ``ENDPOINTS`` is protected from that day on. Anything left
out is not.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from tests.integration.conftest import auth_header, client_for, token_for
from tests.integration.test_review_api import seeded  # noqa: F401

pytestmark = pytest.mark.integration

Call = Callable[[], Awaitable[httpx.Response]]

#: Small enough that one endpoint wanting two connections deadlocks on the
#: first try, and short enough that the failure is a fast red rather than a
#: minute of nothing.
POOL_SIZE = 2
POOL_TIMEOUT_SECONDS = 2

ENDPOINTS = (
    "GET /me/entitlements",
    "GET /courses",
    "POST /lessons/{id}/start",
    "GET /lessons/{id}",
    "POST /lessons/{id}/complete",
    "GET /review/queue",
    "POST /payments/checkout",
)


def endpoints(
    client: httpx.AsyncClient, headers: dict[str, str], lesson_id: uuid.UUID
) -> dict[str, Call]:
    """Every authenticated endpoint a learner can reach, by name."""
    return {
        "GET /me/entitlements": lambda: client.get("/api/v1/me/entitlements", headers=headers),
        "GET /courses": lambda: client.get("/api/v1/courses", headers=headers),
        "POST /lessons/{id}/start": lambda: client.post(
            f"/api/v1/lessons/{lesson_id}/start", headers=headers
        ),
        "GET /lessons/{id}": lambda: client.get(f"/api/v1/lessons/{lesson_id}", headers=headers),
        "POST /lessons/{id}/complete": lambda: client.post(
            f"/api/v1/lessons/{lesson_id}/complete", headers=headers
        ),
        "GET /review/queue": lambda: client.get("/api/v1/review/queue", headers=headers),
        "POST /payments/checkout": lambda: client.post(
            "/api/v1/payments/checkout",
            headers=headers,
            json={"plan": "pro", "period": "monthly", "currency": "USD"},
        ),
    }


@pytest.fixture
def cramped(settings: Settings) -> Settings:
    """The suite's own configuration, with the pool squeezed down."""
    values: dict[str, Any] = {
        **settings.model_dump(),
        "db_pool_size": POOL_SIZE,
        "db_max_overflow": 0,
        "db_pool_timeout_seconds": POOL_TIMEOUT_SECONDS,
    }
    return Settings.model_construct(**values)


@pytest.fixture
async def cramped_client(cramped: Settings) -> AsyncIterator[httpx.AsyncClient]:
    async with client_for(cramped) as http:
        yield http


@pytest.mark.parametrize("name", ENDPOINTS)
async def test_an_endpoint_answers_when_the_pool_is_full(
    name: str,
    cramped_client: httpx.AsyncClient,
    seeded: dict[str, uuid.UUID],  # noqa: F811
) -> None:
    at_once = POOL_SIZE + 1
    calls = [
        endpoints(
            cramped_client,
            auth_header(await token_for(cramped_client, telegram_id=900_000 + index)),
            seeded["lesson"],
        )[name]
        for index in range(at_once)
    ]

    responses = await asyncio.gather(*(call() for call in calls), return_exceptions=True)

    stalled = [r for r in responses if not isinstance(r, httpx.Response)]
    assert not stalled, f"{name} could not answer {at_once} at once: {stalled[0]!r}"

    codes = {r.status_code for r in responses if isinstance(r, httpx.Response)}
    assert max(codes) < 500, f"{name} returned {sorted(codes)}"
