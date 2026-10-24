"""The one HTTP call both Azure adapters make, and what can go wrong with it.

REST rather than the Speech SDK: the SDK is a native library with its own
audio stack, and two verification scripts that send a few dozen requests in
their lifetime do not justify a binary dependency. httpx is already in the
backend.

The interfaces forbid raising, so every way a request can fail comes back as
an ``Outcome`` with a short machine-readable code. The adapters prefix it
(``tts.`` / ``scoring.``) so a failure in a report names its side.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Final

import httpx

#: The resource is on the free tier (F0), which throttles to roughly twenty
#: requests a minute and answers 429 beyond that. A blind-test export is
#: thirty sequential requests, so it will meet that limit; giving up on the
#: first 429 would make the run fail for a reason that is not about Khmer.
#: Not configuration: nothing about real usage would tune a verification
#: script's patience (CODING_STANDARDS section 4).
MAX_ATTEMPTS: Final = 3
MAX_WAIT_SECONDS: Final = 60.0

#: How much of an error body to keep. Enough to read Azure's reason, short
#: enough that a report row stays a row.
_DETAIL_CHARS: Final = 200

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class Outcome:
    """A response worth reading, or why there is none."""

    response: httpx.Response | None = None
    #: ``timeout`` / ``network`` / ``rate_limited`` / ``auth_failed`` / ``http_error``
    error: str | None = None
    message: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


async def post(
    url: str,
    *,
    headers: Mapping[str, str],
    content: bytes,
    timeout_seconds: float,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Sleep = asyncio.sleep,
) -> Outcome:
    """POST once, waiting out a 429 up to ``MAX_ATTEMPTS`` times.

    Only 429 is retried. A timeout may already have been billed, and a 4xx
    other than 429 will say the same thing the second time.
    """
    async with httpx.AsyncClient(transport=transport, timeout=timeout_seconds) as client:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await client.post(url, headers=dict(headers), content=content)
            except httpx.TimeoutException:
                return Outcome(error="timeout", message=f"no response within {timeout_seconds}s")
            except httpx.HTTPError as error:
                # The type name, not str(error): httpx messages can carry the
                # request, and the request carries the key.
                return Outcome(error="network", message=type(error).__name__)
            if response.status_code == 429 and attempt < MAX_ATTEMPTS:
                await sleep(_wait_seconds(response, attempt))
                continue
            return _classify(response)
    raise AssertionError("unreachable: the loop always returns")  # pragma: no cover


def _wait_seconds(response: httpx.Response, attempt: int) -> float:
    """Azure's Retry-After when it sends one, otherwise doubling from 2s."""
    try:
        wait = float(response.headers.get("Retry-After", ""))
    except ValueError:
        wait = float(2**attempt)
    return min(max(wait, 0.0), MAX_WAIT_SECONDS)


def _classify(response: httpx.Response) -> Outcome:
    status = response.status_code
    if status == 200:
        return Outcome(response=response)
    detail = f"HTTP {status}: {response.text[:_DETAIL_CHARS]}".rstrip(": ")
    if status in (401, 403):
        return Outcome(error="auth_failed", message=detail)
    if status == 429:
        return Outcome(error="rate_limited", message=detail)
    return Outcome(error="http_error", message=detail)


__all__ = ["MAX_ATTEMPTS", "MAX_WAIT_SECONDS", "Outcome", "Sleep", "post"]
