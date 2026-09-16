"""Chooses a text LLM by configuration.

There is no routing axis here. Scoring routes by language and TTS by voice
because vendors genuinely differ along those; a text model handles whatever it
is given, so LLM_PROVIDER resolves directly.

Purpose is not a routing key either — base.py says the provider uses it to pick
a model tier and attribute cost, which is a decision inside an implementation,
not a choice between implementations.
"""

from __future__ import annotations

from collections.abc import Callable

from app.core.config import Settings
from app.services.llm.base import LLMProvider
from app.services.llm.fake import FakeLLM

_FACTORIES: dict[str, Callable[[], LLMProvider]] = {
    "fake": FakeLLM,
}


def available_providers() -> tuple[str, ...]:
    return tuple(sorted(_FACTORIES))


def build_llm(name: str) -> LLMProvider:
    """Instantiate one provider by name.

    Raises:
        ValueError: no such implementation — a typo in LLM_PROVIDER, or a vendor
            that has not landed yet.
    """
    factory = _FACTORIES.get(name)
    if factory is None:
        raise ValueError(
            f"unknown llm provider {name!r}; implemented: {list(available_providers())}"
        )
    return factory()


def get_llm(*, settings: Settings) -> LLMProvider:
    """The configured provider.

    Returns a fresh instance per call: implementations may carry per-session
    state — FakeLLM tracks which system prompts it has already seen, to model
    prompt caching — and sharing that across unrelated callers would make cache
    hits depend on who ran first.
    """
    return build_llm(settings.llm_provider)
