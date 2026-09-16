"""Deterministic stand-in for a text LLM.

Two callers, with opposite needs, and this has to serve both (base.py's Purpose):

  · Offline, in pipeline/generate.py, where the caller hands over a json_schema
    and needs something structurally valid back so the rest of the pipeline can
    run before any real generation exists.
  · At runtime, for Q&A replies and correction notes — the only place LLM text
    is allowed at all (red line R1).

Schema handling is deliberately narrow. Rather than guess at a construct it
does not implement, it returns ok=False and names the construct: the base
contract promises that a non-empty json_schema yields parseable output or an
explicit failure, and quietly emitting something that does not match the schema
would break that promise in the way that is hardest to notice.

Prompt caching is modelled because the contract asks for it. A system prompt
repeated verbatim reports its tokens as cached; change one character and the
hit disappears. That turns PRD 4.2's "system prompt must be byte-stable" from a
comment into something a test can assert.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Final

from app.services.llm.base import CompletionResult, LLMProvider, Message, Purpose

#: Rough characters per token. Only used for plausible token counts; nothing
#: about real traffic would change it, so it is not configuration.
_CHARS_PER_TOKEN: Final = 4

#: Blended ~$1 per million tokens, so a Q&A turn costs a fraction of a cent and
#: integer division reports 0. Same reasoning as FakeTTS — see D-008.
_TOKENS_PER_USD_CENT: Final = 10_000

#: An array with no minItems is minimally satisfied by being empty, but an empty
#: placeholder gives downstream tests nothing to inspect. One specimen is the floor.
_DEFAULT_ARRAY_ITEMS: Final = 1


class UnsupportedSchemaError(Exception):
    """A json_schema construct this fake does not implement."""


def _resolve(node: Mapping[str, Any], root: Mapping[str, Any]) -> Mapping[str, Any]:
    """Follow a local $ref. Remote refs would mean network IO in a fake."""
    ref = node.get("$ref")
    if ref is None:
        return node
    if not isinstance(ref, str) or not ref.startswith("#/"):
        raise UnsupportedSchemaError(f"only local $ref is supported, got {ref!r}")

    target: Any = root
    for part in ref.removeprefix("#/").split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(target, Mapping) or key not in target:
            raise UnsupportedSchemaError(f"$ref {ref!r} does not resolve")
        target = target[key]
    if not isinstance(target, Mapping):
        raise UnsupportedSchemaError(f"$ref {ref!r} does not point at a schema")
    return _resolve(target, root)


def _placeholder_string(path: str, node: Mapping[str, Any]) -> str:
    """Readable, deterministic, and inside any length bounds the schema sets."""
    value = f"fake:{path}" if path else "fake"

    minimum = node.get("minLength")
    if isinstance(minimum, int) and len(value) < minimum:
        value += "x" * (minimum - len(value))

    maximum = node.get("maxLength")
    if isinstance(maximum, int) and len(value) > maximum:
        value = value[:maximum]
    return value


def minimal_instance(
    schema: Mapping[str, Any], *, root: Mapping[str, Any] | None = None, path: str = ""
) -> Any:
    """Smallest value satisfying ``schema``.

    Raises:
        UnsupportedSchemaError: a construct this fake does not implement. Callers
            turn it into ok=False rather than returning something unchecked.
    """
    root = root if root is not None else schema
    node = _resolve(schema, root)

    if "const" in node:
        return node["const"]

    enum = node.get("enum")
    if isinstance(enum, Sequence) and not isinstance(enum, str):
        if not enum:
            raise UnsupportedSchemaError(f"empty enum at {path or '<root>'}")
        return enum[0]

    # A union is satisfied by any one branch, so take the first.
    for keyword in ("anyOf", "oneOf"):
        branches = node.get(keyword)
        if isinstance(branches, Sequence) and not isinstance(branches, str):
            if not branches:
                raise UnsupportedSchemaError(f"empty {keyword} at {path or '<root>'}")
            return minimal_instance(branches[0], root=root, path=path)

    declared = node.get("type")
    if isinstance(declared, list):
        # A type union; the first entry is as good as any.
        declared = declared[0] if declared else None
    if declared is None:
        raise UnsupportedSchemaError(
            f"no type, const, enum or union at {path or '<root>'}; "
            "this fake will not guess what shape to emit"
        )

    if declared == "object":
        return _object_instance(node, root=root, path=path)
    if declared == "array":
        return _array_instance(node, root=root, path=path)
    if declared == "string":
        return _placeholder_string(path, node)
    if declared in ("integer", "number"):
        low = node.get("minimum", node.get("exclusiveMinimum"))
        value = low if isinstance(low, int | float) else 0
        if "exclusiveMinimum" in node and value == node["exclusiveMinimum"]:
            value += 1
        return int(value) if declared == "integer" else float(value)
    if declared == "boolean":
        return False
    if declared == "null":
        return None

    raise UnsupportedSchemaError(f"unsupported type {declared!r} at {path or '<root>'}")


def _object_instance(
    node: Mapping[str, Any], *, root: Mapping[str, Any], path: str
) -> dict[str, Any]:
    """Required properties, or all of them when nothing is required.

    An object with only optional properties is minimally satisfied by {}, but an
    empty placeholder is useless to the pipeline stage reading it, so in that
    case every declared property is filled in.
    """
    properties = node.get("properties")
    if not isinstance(properties, Mapping):
        return {}

    required = node.get("required")
    wanted = (
        [name for name in required if name in properties]
        if isinstance(required, Sequence) and not isinstance(required, str)
        else list(properties)
    )

    return {
        name: minimal_instance(properties[name], root=root, path=f"{path}.{name}" if path else name)
        for name in wanted
    }


def _array_instance(node: Mapping[str, Any], *, root: Mapping[str, Any], path: str) -> list[Any]:
    items = node.get("items")
    if not isinstance(items, Mapping):
        raise UnsupportedSchemaError(
            f"array at {path or '<root>'} has no single items schema; "
            "tuple-form items are not implemented"
        )

    count = node.get("minItems")
    count = count if isinstance(count, int) and count > 0 else _DEFAULT_ARRAY_ITEMS
    maximum = node.get("maxItems")
    if isinstance(maximum, int):
        count = min(count, maximum)

    return [minimal_instance(items, root=root, path=f"{path}.{index}") for index in range(count)]


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN) if text else 0


def _last_user_message(messages: Sequence[Message]) -> str | None:
    for message in reversed(messages):
        if message.role == "user":
            return message.content
    return None


def _system_text(messages: Sequence[Message]) -> str:
    return "".join(m.content for m in messages if m.role == "system")


class FakeLLM(LLMProvider):
    """Echoes, or fills in a schema. Never calls out, never invents prose."""

    name = "fake"

    def __init__(self) -> None:
        #: System prompts this instance has already seen, standing in for the
        #: vendor's prompt cache. Per-instance so a test starts cold.
        self._cached_system_prompts: set[str] = set()

    async def complete(
        self,
        messages: list[Message],
        *,
        purpose: Purpose,
        max_output_tokens: int = 512,
        temperature: float = 0.3,
        json_schema: dict[str, Any] | None = None,
    ) -> CompletionResult:
        if not messages:
            return _failure("llm.no_messages", "messages is empty")

        system = _system_text(messages)
        cached = _estimate_tokens(system) if system in self._cached_system_prompts else 0
        if system:
            self._cached_system_prompts.add(system)

        if json_schema is not None:
            try:
                parsed = minimal_instance(json_schema)
            except UnsupportedSchemaError as exc:
                # Better a named failure than output that silently misses the
                # schema the caller is about to trust.
                return _failure("llm.schema_unsupported", str(exc))
            text = json.dumps(parsed, ensure_ascii=False, sort_keys=True)
        else:
            echo = _last_user_message(messages)
            if echo is None:
                return _failure("llm.no_user_message", "no user message to echo")
            parsed = None
            text = echo

        text = self._truncate(text, max_output_tokens)
        input_tokens = _estimate_tokens("".join(m.content for m in messages))
        output_tokens = _estimate_tokens(text)

        return CompletionResult(
            ok=True,
            text=text,
            parsed=parsed,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached,
            model=f"{self.name}-{purpose.value}",
            cost_usd_cents=(input_tokens + output_tokens) // _TOKENS_PER_USD_CENT,
        )

    async def batch_complete(
        self,
        batches: list[list[Message]],
        *,
        purpose: Purpose = Purpose.OFFLINE_GENERATION,
        max_output_tokens: int = 2048,
        temperature: float = 0.7,
        json_schema: dict[str, Any] | None = None,
    ) -> list[CompletionResult]:
        """One result per input, in order, failures included.

        generate.py pairs results back to seeds positionally, so dropping a
        failed item would silently shift every concept after it onto the wrong
        content — which is why the contract forbids it.
        """
        return [
            await self.complete(
                batch,
                purpose=purpose,
                max_output_tokens=max_output_tokens,
                temperature=temperature,
                json_schema=json_schema,
            )
            for batch in batches
        ]

    def _truncate(self, text: str, max_output_tokens: int) -> str:
        """Respect the caller's ceiling the way a real provider would."""
        limit = max(0, max_output_tokens) * _CHARS_PER_TOKEN
        return text[:limit]


def _failure(code: str, message: str) -> CompletionResult:
    return CompletionResult(ok=False, error_code=code, error_message=message, cost_usd_cents=0)


__all__ = ["FakeLLM", "UnsupportedSchemaError", "minimal_instance"]
