"""A scripted stand-in for Azure Speech, shared by every test of an Azure adapter.

Not a test module (no ``test_`` prefix). Every request goes to an httpx
MockTransport: nothing in the suite touches the network.
"""

from __future__ import annotations

import httpx

KEY = "test-key-not-a-real-one"


class Recorder:
    """A transport that answers from a script and remembers what it was sent."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self._responses.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


async def no_sleep(_: float) -> None:
    return None
