"""The OpenAI adapter, against a stubbed wire (BACKLOG E3 / D-103).

There is no API key in this repository and there will not be one in CI, so
every test here drives an httpx transport that answers from a script. That
proves the adapter's half of the contract — what it sends, what it makes of
what comes back, and that a failure is a value rather than an exception — and
proves nothing about the vendor. The first real call is a thing a person has
to watch happen; D-103 says so rather than letting a green suite imply it.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.services.llm.base import Message, Purpose
from app.services.llm.openai import OpenAILLM


def settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "DATABASE_URL": "postgresql://unused/unused",
        "REDIS_URL": "redis://unused",
        "TELEGRAM_BOT_TOKEN": "",
        "AZURE_SPEECH_KEY": "",
        "AZURE_SPEECH_REGION": "",
        "GOOGLE_APPLICATION_CREDENTIALS": "",
        "ELEVENLABS_API_KEY": "",
        "OPENAI_API_KEY": "sk-test",
        "PAYWAY_MERCHANT_ID": "",
        "PAYWAY_API_KEY": "",
        "PAYWAY_BASE_URL": "",
        "BAKONG_TOKEN": "",
        "JWT_SECRET": "x" * 32,
        "LLM_RETRY_BASE_SECONDS": "0.001",
        **overrides,
    }
    return Settings(_env_file=None, **base)


def completion(
    content: str = "hello",
    *,
    prompt: int = 100,
    completion_tokens: int = 50,
    cached: int = 0,
    finish_reason: str = "stop",
) -> dict[str, Any]:
    return {
        "model": "gpt-4.1-mini-2026-01-01",
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion_tokens,
            "prompt_tokens_details": {"cached_tokens": cached},
        },
    }


def provider_answering(
    *responses: httpx.Response | dict[str, Any] | Exception, **overrides: Any
) -> tuple[OpenAILLM, list[httpx.Request]]:
    """An adapter wired to a scripted transport, plus the requests it made."""
    seen: list[httpx.Request] = []
    scripted = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        answer = scripted.pop(0) if len(scripted) > 1 else scripted[0]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=answer)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.test/v1"
    )
    return OpenAILLM(settings(**overrides), client=client), seen


SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer"],
    "properties": {"answer": {"type": "string"}},
}


# ------------------------------------------------------------ what it sends


async def test_the_model_and_messages_go_out_as_configured() -> None:
    provider, seen = provider_answering(completion(), LLM_MODEL="gpt-4.1-mini")

    await provider.complete(
        [Message(role="system", content="be brief"), Message(role="user", content="hi")],
        purpose=Purpose.RUNTIME_DIALOGUE,
        max_output_tokens=256,
        temperature=0.2,
    )
    sent = json.loads(seen[0].content)

    assert seen[0].url.path.endswith("/chat/completions")
    assert sent["model"] == "gpt-4.1-mini"
    assert sent["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hi"},
    ]
    assert sent["max_completion_tokens"] == 256
    assert sent["temperature"] == 0.2


async def test_the_key_never_appears_anywhere_but_the_header() -> None:
    """R5: a credential in a log line or a body is a credential that leaks."""
    provider, seen = provider_answering(completion())

    await provider.complete([Message(role="user", content="hi")], purpose=Purpose.RUNTIME_DIALOGUE)

    assert seen[0].headers["Authorization"] == "Bearer sk-test"
    assert "sk-test" not in seen[0].content.decode()


async def test_a_schema_is_sent_as_strict_structured_output() -> None:
    """base.py promises parseable output or an explicit failure; strict is how."""
    provider, seen = provider_answering(completion('{"answer": "yes"}'))

    await provider.complete(
        [Message(role="user", content="hi")],
        purpose=Purpose.OFFLINE_GENERATION,
        json_schema=SCHEMA,
    )
    sent = json.loads(seen[0].content)

    assert sent["response_format"]["type"] == "json_schema"
    assert sent["response_format"]["json_schema"]["strict"] is True
    assert sent["response_format"]["json_schema"]["schema"] == SCHEMA


async def test_no_schema_means_no_response_format() -> None:
    provider, seen = provider_answering(completion())

    await provider.complete([Message(role="user", content="hi")], purpose=Purpose.RUNTIME_DIALOGUE)

    assert "response_format" not in json.loads(seen[0].content)


# --------------------------------------------------------- what it makes of it


async def test_a_good_answer_carries_its_usage() -> None:
    provider, _ = provider_answering(completion("hello", prompt=1000, completion_tokens=500))

    result = await provider.complete(
        [Message(role="user", content="hi")], purpose=Purpose.RUNTIME_DIALOGUE
    )

    assert result.ok
    assert result.text == "hello"
    assert (result.input_tokens, result.output_tokens) == (1000, 500)
    assert result.model == "gpt-4.1-mini-2026-01-01"


async def test_structured_output_comes_back_parsed() -> None:
    provider, _ = provider_answering(completion('{"answer": "yes"}'))

    result = await provider.complete(
        [Message(role="user", content="hi")],
        purpose=Purpose.OFFLINE_GENERATION,
        json_schema=SCHEMA,
    )

    assert result.parsed == {"answer": "yes"}


async def test_cached_input_is_reported_so_a_cache_miss_is_visible() -> None:
    """PRD 4.2 wants a byte-stable system prompt; this is how anyone can tell."""
    provider, _ = provider_answering(completion(prompt=1000, cached=800))

    result = await provider.complete(
        [Message(role="user", content="hi")], purpose=Purpose.RUNTIME_DIALOGUE
    )

    assert result.cached_input_tokens == 800


async def test_cost_is_whole_cents_and_prices_cached_input_lower() -> None:
    """CLAUDE.md section 8: every call is accounted for, in integer cents."""
    dear, _ = provider_answering(
        completion(prompt=1_000_000, completion_tokens=1_000_000, cached=0)
    )
    cheap, _ = provider_answering(
        completion(prompt=1_000_000, completion_tokens=1_000_000, cached=1_000_000)
    )

    full = (
        await dear.complete([Message(role="user", content="x")], purpose=Purpose.RUNTIME_DIALOGUE)
    ).cost_usd_cents
    cached = (
        await cheap.complete([Message(role="user", content="x")], purpose=Purpose.RUNTIME_DIALOGUE)
    ).cost_usd_cents

    assert full == int((0.40 + 1.60) * 100)
    assert cached == int((0.10 + 1.60) * 100)
    assert isinstance(full, int)


async def test_a_single_cheap_call_rounds_to_nothing_rather_than_a_cent() -> None:
    """Rounding each call up would overstate a level by more than it costs."""
    provider, _ = provider_answering(completion(prompt=500, completion_tokens=2500))

    result = await provider.complete(
        [Message(role="user", content="x")], purpose=Purpose.OFFLINE_GENERATION
    )

    assert result.cost_usd_cents == 0


# ------------------------------------------------------------------ failures


async def test_a_refusal_is_a_value_not_an_exception() -> None:
    """base.py: provider failures come back as ok=False so the caller degrades."""
    provider, _ = provider_answering(httpx.Response(400, text="bad request"))

    result = await provider.complete(
        [Message(role="user", content="hi")], purpose=Purpose.RUNTIME_DIALOGUE
    )

    assert result.ok is False
    assert result.error_code == "llm.http_400"


async def test_a_rate_limit_is_retried_and_then_succeeds() -> None:
    provider, seen = provider_answering(httpx.Response(429), completion("eventually"))

    result = await provider.complete(
        [Message(role="user", content="hi")], purpose=Purpose.RUNTIME_DIALOGUE
    )

    assert result.ok
    assert result.text == "eventually"
    assert len(seen) == 2


async def test_a_bad_request_is_not_retried() -> None:
    """It will be just as wrong the second time, and the second time is billed."""
    provider, seen = provider_answering(httpx.Response(400, text="nope"))

    await provider.complete([Message(role="user", content="hi")], purpose=Purpose.RUNTIME_DIALOGUE)

    assert len(seen) == 1


async def test_a_network_failure_gives_up_after_the_configured_retries() -> None:
    provider, seen = provider_answering(httpx.ConnectError("no route"), LLM_MAX_RETRIES="2")

    result = await provider.complete(
        [Message(role="user", content="hi")], purpose=Purpose.RUNTIME_DIALOGUE
    )

    assert result.error_code == "llm.unreachable"
    assert len(seen) == 3


async def test_output_cut_short_is_a_failure_not_half_an_object() -> None:
    """Truncated JSON would parse as nothing or, worse, as something."""
    provider, _ = provider_answering(completion('{"answer": "ye', finish_reason="length"))

    result = await provider.complete(
        [Message(role="user", content="hi")],
        purpose=Purpose.OFFLINE_GENERATION,
        json_schema=SCHEMA,
    )

    assert result.error_code == "llm.truncated"


async def test_output_that_is_not_json_is_a_named_failure() -> None:
    provider, _ = provider_answering(completion("sorry, I cannot"))

    result = await provider.complete(
        [Message(role="user", content="hi")],
        purpose=Purpose.OFFLINE_GENERATION,
        json_schema=SCHEMA,
    )

    assert result.error_code == "llm.unparseable"


async def test_an_empty_message_list_is_refused_without_a_call() -> None:
    provider, seen = provider_answering(completion())

    result = await provider.complete([], purpose=Purpose.RUNTIME_DIALOGUE)

    assert result.error_code == "llm.no_messages"
    assert seen == []


# -------------------------------------------------------------------- batch


async def test_batch_answers_one_for_one_in_order() -> None:
    """The contract the pipeline relies on to line results up with concepts."""
    provider, _ = provider_answering(completion("first"), completion("second"), completion("third"))

    results = await provider.batch_complete(
        [[Message(role="user", content=str(n))] for n in range(3)]
    )

    assert [r.text for r in results] == ["first", "second", "third"]


async def test_a_failure_inside_a_batch_is_a_placeholder_not_a_gap() -> None:
    """Dropping it would misfile every concept after the failure."""
    provider, _ = provider_answering(
        completion("first"), httpx.Response(400, text="no"), completion("third")
    )

    results = await provider.batch_complete(
        [[Message(role="user", content=str(n))] for n in range(3)]
    )

    assert len(results) == 3
    assert [r.ok for r in results] == [True, False, True]


async def test_an_empty_batch_makes_no_calls() -> None:
    provider, seen = provider_answering(completion())

    assert await provider.batch_complete([]) == []
    assert seen == []


async def test_no_more_calls_are_in_flight_than_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """[$] The ceiling on a paid API is the owner's to set (D-103), so it has
    to be the ceiling that is actually applied."""
    in_flight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            import asyncio

            await asyncio.sleep(0.01)
            return httpx.Response(200, json=completion())
        finally:
            in_flight -= 1

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.test/v1"
    )
    provider = OpenAILLM(settings(LLM_MAX_CONCURRENCY="2"), client=client)

    await provider.batch_complete([[Message(role="user", content=str(n))] for n in range(8)])

    assert peak <= 2
