"""The HTTP layer every Azure Speech adapter shares (app/services/azure_speech.py).

The free tier throttles at about twenty requests a minute, so a 429 has to be
waited out rather than reported as a failure about the content. Every other
failure comes back as an outcome, never an exception: the provider interfaces
forbid raising.
"""

from __future__ import annotations

import httpx
import pytest

from app.services.azure_speech import MAX_ATTEMPTS, MAX_WAIT_SECONDS, post
from tests.unit.azure_recorder import Recorder, no_sleep


async def test_a_429_is_waited_out_and_retried() -> None:
    """The free tier throttles at about twenty a minute; a thirty-clip export
    meets that, and failing there would be a failure about nothing."""
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    recorder = Recorder(httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200))

    outcome = await post(
        "https://x",
        headers={},
        content=b"",
        timeout_seconds=1,
        transport=recorder.transport,
        sleep=sleep,
    )

    assert outcome.ok
    assert waits == [7.0]
    assert len(recorder.requests) == 2


async def test_retrying_gives_up_and_says_rate_limited() -> None:
    recorder = Recorder(*[httpx.Response(429) for _ in range(MAX_ATTEMPTS)])

    outcome = await post(
        "https://x",
        headers={},
        content=b"",
        timeout_seconds=1,
        transport=recorder.transport,
        sleep=no_sleep,
    )

    assert outcome.error == "rate_limited"
    assert len(recorder.requests) == MAX_ATTEMPTS


async def test_an_absurd_retry_after_is_capped() -> None:
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    recorder = Recorder(httpx.Response(429, headers={"Retry-After": "86400"}), httpx.Response(200))
    await post(
        "https://x",
        headers={},
        content=b"",
        timeout_seconds=1,
        transport=recorder.transport,
        sleep=sleep,
    )

    assert waits == [MAX_WAIT_SECONDS]


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (httpx.Response(401), "auth_failed"),
        (httpx.Response(403), "auth_failed"),
        (httpx.Response(400, text="bad ssml"), "http_error"),
        (httpx.ReadTimeout("slow"), "timeout"),
        (httpx.ConnectError("down"), "network"),
    ],
)
async def test_every_failure_is_an_outcome_not_an_exception(
    answer: httpx.Response | Exception, error: str
) -> None:
    """The interfaces forbid raising, and only one retry case is worth it:
    a 401 or a malformed request will say the same thing the second time."""
    recorder = Recorder(answer)

    outcome = await post(
        "https://x",
        headers={},
        content=b"",
        timeout_seconds=1,
        transport=recorder.transport,
        sleep=no_sleep,
    )

    assert outcome.error == error
    assert len(recorder.requests) == 1
