"""OpenAI-compatible text LLM, over plain HTTP.

The first real provider in the repository. Two choices in it are worth stating
before the code, because both look like shortcuts and are not.

**httpx, not the vendor SDK.** Red line R2 is about never bypassing the
abstraction, and an SDK behind ``base.py`` would not bypass it — but one
endpoint does not need a dependency, and keeping the wire format visible is
what makes "point this at another vendor's compatible endpoint" a configuration
change rather than a rewrite. ``LLM_BASE_URL`` exists for exactly that.

**No Batch API.** base.py says to use the vendor's Batch API where possible,
because it is half the price. Priced out, that saving is about ten cents for a
whole HSK1 level (docs/DECISIONS.md D-103) and it costs a 24-hour turnaround on
every run — which would turn generate → validate → fix from an afternoon into a
week. ``batch_complete`` therefore runs ordinary calls with a bounded number in
flight. If a level ever costs enough for ten cents to matter, the saving will
still be there.

Nothing here decides to spend money: ``pipeline/generate.py`` refuses to run a
billed provider without ``--confirm-spend`` (D-085), and CI is all-fake.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

import httpx

from app.core.config import Settings
from app.core.logging import get_logger
from app.services.llm.base import CompletionResult, LLMProvider, Message, Purpose

log = get_logger(__name__)

#: Tokens are priced per million; costs are recorded in whole USD cents
#: (CODING_STANDARDS section 3). A single call rounds to zero cents and that is
#: correct — the ledger sums them, and claiming a cent per call would overstate
#: a level's cost by more than the level costs.
_TOKENS_PER_MILLION: Final = 1_000_000
_CENTS_PER_USD: Final = 100

#: Worth another attempt: rate limiting and the gateway family. A 400 means the
#: request is malformed and will be malformed again.
_RETRYABLE: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})


class OpenAILLM(LLMProvider):
    """Chat completions with structured output.

    Not registered by default: LLM_PROVIDER stays ``fake`` until somebody sets
    it, and ``registry.py`` builds this only when it is asked for.
    """

    name = "openai"

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._model = settings.llm_model
        self._client = client
        self._owns_client = client is None

    # ------------------------------------------------------------------ http

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[httpx.AsyncClient]:
        """One client for the span of a call, or the injected one.

        Explicit rather than a long-lived client on the instance: the registry
        hands out a fresh provider per call, so an instance-level client would
        be opened and never closed. Tests inject one and keep it.
        """
        if self._client is not None:
            yield self._client
            return
        async with httpx.AsyncClient(
            base_url=self._settings.llm_base_url,
            timeout=httpx.Timeout(self._settings.llm_timeout_seconds),
        ) as client:
            yield client

    def _headers(self) -> dict[str, str]:
        """Auth goes on the request, not on the client.

        A header set when the client is built is a header an injected client
        does not have — a test, a proxy, a future shared session — and the
        symptom would be an unauthenticated call that looks fine here.
        """
        key = self._settings.openai_api_key.get_secret_value()
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    async def _post(
        self, client: httpx.AsyncClient, payload: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str, str]:
        """One request, with retries. Returns (body, error_code, error_message).

        Never raises: base.py's contract is that a provider failure comes back
        as ok=False, so the caller decides what to degrade to.
        """
        retries = self._settings.llm_max_retries
        for attempt in range(retries + 1):
            try:
                response = await client.post(
                    "/chat/completions", json=payload, headers=self._headers()
                )
            except httpx.HTTPError as error:
                if attempt >= retries:
                    return None, "llm.unreachable", str(error)
                await self._backoff(attempt)
                continue

            if response.status_code == 200:
                return response.json(), "", ""
            # 429 and 5xx are worth another go; a 400 means the request is
            # wrong and will be wrong again.
            if response.status_code in _RETRYABLE and attempt < retries:
                await self._backoff(attempt)
                continue
            return None, f"llm.http_{response.status_code}", response.text[:500]
        return None, "llm.unreachable", "retries exhausted"  # pragma: no cover

    async def _backoff(self, attempt: int) -> None:
        await asyncio.sleep(self._settings.llm_retry_base_seconds * (2**attempt))

    # ------------------------------------------------------------- contract

    async def complete(
        self,
        messages: list[Message],
        *,
        purpose: Purpose,
        max_output_tokens: int = 512,
        temperature: float = 0.3,
        json_schema: dict[str, Any] | None = None,
    ) -> CompletionResult:
        async with self._session() as client:
            return await self._one(
                client,
                messages,
                max_output_tokens=max_output_tokens,
                temperature=temperature,
                json_schema=json_schema,
            )

    async def _one(
        self,
        client: httpx.AsyncClient,
        messages: list[Message],
        *,
        max_output_tokens: int,
        temperature: float,
        json_schema: dict[str, Any] | None,
    ) -> CompletionResult:
        if not messages:
            return _failure("llm.no_messages", "messages is empty")

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_completion_tokens": max_output_tokens,
            "temperature": temperature,
        }
        if json_schema is not None:
            # strict mode is what makes the promise in base.py keepable: either
            # the output matches the schema or the call fails.
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "content", "strict": True, "schema": json_schema},
            }

        body, error_code, error_message = await self._post(client, payload)
        if body is None:
            log.warning("llm.failed", provider=self.name, error_code=error_code)
            return _failure(error_code, error_message)

        return self._result(body, structured=json_schema is not None)

    def _result(self, body: dict[str, Any], *, structured: bool = False) -> CompletionResult:
        usage = body.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens", 0))
        output_tokens = int(usage.get("completion_tokens", 0))
        cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0))

        choices = body.get("choices") or []
        if not choices:
            return _failure("llm.no_choices", "the response carried no completion")
        message = choices[0].get("message") or {}
        if choices[0].get("finish_reason") == "length":
            # Truncated JSON parses as nothing useful; say why rather than
            # returning half an object.
            return _failure("llm.truncated", "the model hit max_completion_tokens")
        text = message.get("content") or ""

        parsed: dict[str, Any] | None = None
        if structured:
            try:
                loaded = json.loads(text)
            except json.JSONDecodeError as error:
                return _failure("llm.unparseable", f"structured output is not JSON: {error}")
            if not isinstance(loaded, dict):
                return _failure("llm.unparseable", "structured output is not an object")
            parsed = loaded

        return CompletionResult(
            ok=True,
            text=text,
            parsed=parsed,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached,
            model=str(body.get("model") or self._model),
            cost_usd_cents=self._cost(input_tokens, output_tokens, cached),
        )

    def _cost(self, input_tokens: int, output_tokens: int, cached: int) -> int:
        """Whole cents, rounded down, with cached input charged at its own rate."""
        settings = self._settings
        fresh = max(input_tokens - cached, 0)
        usd = (
            fresh * settings.llm_price_input_usd_per_million
            + cached * settings.llm_price_cached_input_usd_per_million
            + output_tokens * settings.llm_price_output_usd_per_million
        ) / _TOKENS_PER_MILLION
        return int(usd * _CENTS_PER_USD)

    async def batch_complete(
        self,
        batches: list[list[Message]],
        *,
        purpose: Purpose = Purpose.OFFLINE_GENERATION,
        max_output_tokens: int = 2048,
        temperature: float = 0.7,
        json_schema: dict[str, Any] | None = None,
    ) -> list[CompletionResult]:
        """Every batch, in order, with a bounded number in flight.

        Order is positional, as the contract requires, and a failure comes back
        as an ok=False placeholder rather than a gap — a caller that lined
        results up by index would otherwise misfile everything after the first
        failure.
        """
        if not batches:
            return []

        limit = asyncio.Semaphore(self._settings.llm_max_concurrency)

        async with self._session() as client:

            async def one(messages: list[Message]) -> CompletionResult:
                async with limit:
                    return await self._one(
                        client,
                        messages,
                        max_output_tokens=max_output_tokens,
                        temperature=temperature,
                        json_schema=json_schema,
                    )

            return list(await asyncio.gather(*(one(messages) for messages in batches)))


def _failure(code: str, message: str) -> CompletionResult:
    return CompletionResult(ok=False, error_code=code, error_message=message)
