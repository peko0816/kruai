"""FakeLLM's central claim is that its output satisfies the caller's schema.

That claim is checked with the `jsonschema` validator rather than by asserting
the shape we expected, because the failure mode worth catching is our own
misreading of the spec — an assertion written from the same misreading would
agree with the bug.

The batch ordering tests carry more weight than they look like they do:
generate.py pairs results back to seeds positionally, so a dropped or reordered
failure silently attaches every later concept to the wrong content.
"""

from __future__ import annotations

from typing import Any

import pytest
from jsonschema import Draft202012Validator

from app.services.llm.base import Message, Purpose
from app.services.llm.fake import FakeLLM, UnsupportedSchemaError, minimal_instance

SYSTEM = Message(role="system", content="You are a Khmer-first Chinese tutor.")
USER = Message(role="user", content="我要水")


def conversation(user: str = "我要水", *, system: str | None = None) -> list[Message]:
    messages = [Message(role="system", content=system)] if system else []
    return [*messages, Message(role="user", content=user)]


def assert_valid(instance: Any, schema: dict[str, Any]) -> None:
    """Independent check: does this really satisfy the schema as written?"""
    Draft202012Validator.check_schema(schema)
    errors = sorted(Draft202012Validator(schema).iter_errors(instance), key=str)
    assert not errors, "\n".join(f"{list(e.path)}: {e.message}" for e in errors)


# A shape close to what generate.py will ask for (PRD 6.1).
CONTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["concept_slug", "sentences", "substitutions", "dialogues"],
    "properties": {
        "concept_slug": {"type": "string", "minLength": 3},
        "sentences": {
            "type": "array",
            "minItems": 8,
            "items": {
                "type": "object",
                "required": ["zh", "pinyin", "km_explanation"],
                "properties": {
                    "zh": {"type": "string"},
                    "pinyin": {"type": "string"},
                    "km_explanation": {"type": "string"},
                    "tone_marks": {"type": "array", "items": {"type": "integer"}},
                },
            },
        },
        "substitutions": {"type": "array", "minItems": 12, "items": {"type": "string"}},
        "dialogues": {
            "type": "array",
            "minItems": 3,
            "items": {
                "type": "object",
                "required": ["prompt_km", "expected_zh", "hskk_task_type"],
                "properties": {
                    "prompt_km": {"type": "string"},
                    "expected_zh": {"type": "string"},
                    "hskk_task_type": {"enum": ["repeat", "answer", "describe"]},
                },
            },
        },
    },
}


# ------------------------------------------------- output satisfies the schema


async def test_generated_content_satisfies_the_pipeline_schema() -> None:
    result = await FakeLLM().complete(
        conversation(), purpose=Purpose.OFFLINE_GENERATION, json_schema=CONTENT_SCHEMA
    )
    assert result.ok
    assert result.parsed is not None
    assert_valid(result.parsed, CONTENT_SCHEMA)


async def test_min_items_are_honoured() -> None:
    """validate.py counts these; a fake that emitted one would hide the check."""
    result = await FakeLLM().complete(
        conversation(), purpose=Purpose.OFFLINE_GENERATION, json_schema=CONTENT_SCHEMA
    )
    assert result.parsed is not None
    assert len(result.parsed["sentences"]) == 8
    assert len(result.parsed["substitutions"]) == 12
    assert len(result.parsed["dialogues"]) == 3


async def test_text_is_the_json_serialisation_of_parsed() -> None:
    import json

    result = await FakeLLM().complete(
        conversation(), purpose=Purpose.OFFLINE_GENERATION, json_schema=CONTENT_SCHEMA
    )
    assert json.loads(result.text) == result.parsed


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "string"},
        {"type": "integer"},
        {"type": "number"},
        {"type": "boolean"},
        {"type": "null"},
        {"type": "array", "items": {"type": "string"}},
        {"type": "object", "properties": {"a": {"type": "string"}}},
        {"type": "string", "enum": ["x", "y"]},
        {"const": 42},
        {"type": "string", "minLength": 40},
        {"type": "string", "maxLength": 3},
        {"type": "integer", "minimum": 7},
        {"type": "array", "items": {"type": "integer"}, "minItems": 4, "maxItems": 9},
        {"anyOf": [{"type": "string"}, {"type": "integer"}]},
        {"oneOf": [{"type": "boolean"}]},
        {"type": ["string", "null"]},
    ],
)
def test_every_supported_construct_produces_a_valid_instance(schema: dict[str, Any]) -> None:
    assert_valid(minimal_instance(schema), schema)


def test_local_refs_resolve() -> None:
    schema: dict[str, Any] = {
        "type": "object",
        "required": ["items"],
        "properties": {"items": {"type": "array", "items": {"$ref": "#/$defs/row"}}},
        "$defs": {
            "row": {
                "type": "object",
                "required": ["zh"],
                "properties": {"zh": {"type": "string"}},
            }
        },
    }
    assert_valid(minimal_instance(schema), schema)


def test_placeholders_name_their_own_path() -> None:
    """Readable output matters when a pack build goes wrong at item 400."""
    schema: dict[str, Any] = {
        "type": "object",
        "required": ["pattern"],
        "properties": {"pattern": {"type": "string"}},
    }
    assert minimal_instance(schema) == {"pattern": "fake:pattern"}


def test_object_with_no_required_list_gets_every_property() -> None:
    """{} satisfies the schema but gives the next pipeline stage nothing."""
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
    }
    assert set(minimal_instance(schema)) == {"a", "b"}


# ------------------------------------------- unsupported constructs, not guesses


@pytest.mark.parametrize(
    "schema",
    [
        {},
        {"type": "array"},
        {"type": "array", "items": [{"type": "string"}]},
        {"$ref": "https://example.com/schema.json"},
        {"$ref": "#/$defs/missing"},
        {"enum": []},
        {"type": "unknown-type"},
    ],
)
def test_unhandled_constructs_raise_rather_than_guess(schema: dict[str, Any]) -> None:
    with pytest.raises(UnsupportedSchemaError):
        minimal_instance(schema)


async def test_unsupported_schema_becomes_a_named_failure() -> None:
    """The contract promises parseable output or an explicit failure."""
    result = await FakeLLM().complete(
        conversation(), purpose=Purpose.OFFLINE_GENERATION, json_schema={"type": "array"}
    )
    assert result.ok is False
    assert result.error_code == "llm.schema_unsupported"
    assert result.parsed is None


# ----------------------------------------------------------------- echo mode


async def test_without_a_schema_the_last_user_message_comes_back() -> None:
    result = await FakeLLM().complete([SYSTEM, USER], purpose=Purpose.RUNTIME_DIALOGUE)
    assert result.ok
    assert result.text == "我要水"
    assert result.parsed is None


async def test_the_last_user_message_wins_not_the_first() -> None:
    messages = [USER, Message(role="assistant", content="好"), Message(role="user", content="再说")]
    result = await FakeLLM().complete(messages, purpose=Purpose.RUNTIME_DIALOGUE)
    assert result.text == "再说"


async def test_conversation_with_no_user_turn_is_a_named_failure() -> None:
    result = await FakeLLM().complete([SYSTEM], purpose=Purpose.RUNTIME_DIALOGUE)
    assert result.ok is False
    assert result.error_code == "llm.no_user_message"


async def test_empty_messages_is_a_named_failure() -> None:
    result = await FakeLLM().complete([], purpose=Purpose.RUNTIME_DIALOGUE)
    assert result.ok is False
    assert result.error_code == "llm.no_messages"


async def test_output_respects_the_token_ceiling() -> None:
    long_input = Message(role="user", content="字" * 5000)
    result = await FakeLLM().complete(
        [long_input], purpose=Purpose.RUNTIME_DIALOGUE, max_output_tokens=10
    )
    assert len(result.text) == 40


# ----------------------------------------------------------------- determinism


async def test_same_input_gives_the_same_output() -> None:
    first = await FakeLLM().complete(conversation(), purpose=Purpose.RUNTIME_DIALOGUE)
    second = await FakeLLM().complete(conversation(), purpose=Purpose.RUNTIME_DIALOGUE)
    assert first == second


def test_schema_filling_is_deterministic() -> None:
    assert minimal_instance(CONTENT_SCHEMA) == minimal_instance(CONTENT_SCHEMA)


# -------------------------------------------------------------- prompt caching


async def test_a_repeated_system_prompt_reports_a_cache_hit() -> None:
    """PRD 4.2 makes byte-stable system prompts a cost requirement; this is what
    lets a test assert the prompt did not drift."""
    provider = FakeLLM()
    first = await provider.complete([SYSTEM, USER], purpose=Purpose.RUNTIME_DIALOGUE)
    second = await provider.complete([SYSTEM, USER], purpose=Purpose.RUNTIME_DIALOGUE)

    assert first.cached_input_tokens == 0
    assert second.cached_input_tokens > 0


async def test_one_changed_character_loses_the_cache_hit() -> None:
    provider = FakeLLM()
    await provider.complete([SYSTEM, USER], purpose=Purpose.RUNTIME_DIALOGUE)
    drifted = Message(role="system", content=SYSTEM.content + ".")
    result = await provider.complete([drifted, USER], purpose=Purpose.RUNTIME_DIALOGUE)

    assert result.cached_input_tokens == 0


async def test_a_fresh_provider_starts_cold() -> None:
    await FakeLLM().complete([SYSTEM, USER], purpose=Purpose.RUNTIME_DIALOGUE)
    result = await FakeLLM().complete([SYSTEM, USER], purpose=Purpose.RUNTIME_DIALOGUE)
    assert result.cached_input_tokens == 0


# --------------------------------------------------------- batch correspondence


async def test_batch_returns_one_result_per_input_in_order() -> None:
    """BACKLOG B3 acceptance."""
    batches = [conversation(f"句子{index}") for index in range(5)]
    results = await FakeLLM().batch_complete(batches, purpose=Purpose.OFFLINE_GENERATION)

    assert len(results) == len(batches)
    assert [r.text for r in results] == [f"句子{index}" for index in range(5)]


async def test_a_failing_item_holds_its_place() -> None:
    """Dropping it would shift every later concept onto the wrong content."""
    batches = [conversation("一"), [SYSTEM], conversation("三")]
    results = await FakeLLM().batch_complete(batches, purpose=Purpose.OFFLINE_GENERATION)

    assert len(results) == 3
    assert [r.ok for r in results] == [True, False, True]
    assert results[1].error_code == "llm.no_user_message"
    assert results[2].text == "三"


async def test_an_empty_batch_returns_an_empty_list() -> None:
    assert await FakeLLM().batch_complete([], purpose=Purpose.OFFLINE_GENERATION) == []


async def test_batch_applies_the_schema_to_every_item() -> None:
    batches = [conversation(f"c{index}") for index in range(3)]
    results = await FakeLLM().batch_complete(batches, json_schema=CONTENT_SCHEMA)

    assert all(r.ok for r in results)
    for result in results:
        assert result.parsed is not None
        assert_valid(result.parsed, CONTENT_SCHEMA)


# -------------------------------------------------------------------- contract


async def test_model_records_the_purpose_for_cost_attribution() -> None:
    runtime = await FakeLLM().complete(conversation(), purpose=Purpose.RUNTIME_DIALOGUE)
    offline = await FakeLLM().complete(conversation(), purpose=Purpose.OFFLINE_GENERATION)
    assert runtime.model != offline.model
    assert "runtime_dialogue" in runtime.model


async def test_token_counts_are_populated() -> None:
    result = await FakeLLM().complete([SYSTEM, USER], purpose=Purpose.RUNTIME_DIALOGUE)
    assert result.input_tokens > 0
    assert result.output_tokens > 0


async def test_a_short_turn_costs_nothing_measurable() -> None:
    result = await FakeLLM().complete([SYSTEM, USER], purpose=Purpose.RUNTIME_DIALOGUE)
    assert result.cost_usd_cents == 0


async def test_a_large_generation_does_accrue_cost() -> None:
    bulk = [Message(role="user", content="字" * 80_000)]
    result = await FakeLLM().complete(
        bulk, purpose=Purpose.OFFLINE_GENERATION, max_output_tokens=100_000
    )
    assert result.cost_usd_cents > 0


async def test_failures_carry_no_cost() -> None:
    result = await FakeLLM().complete([], purpose=Purpose.RUNTIME_DIALOGUE)
    assert result.cost_usd_cents == 0
