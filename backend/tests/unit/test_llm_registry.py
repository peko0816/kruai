"""LLM selection has no routing axis — a text model takes whatever it is given."""

from __future__ import annotations

from typing import Any

import pytest

from app.core.config import Settings
from app.services.llm import get_llm
from app.services.llm.base import LLMProvider
from app.services.llm.fake import FakeLLM
from app.services.llm.registry import available_providers, build_llm

_MINIMAL: dict[str, str] = {
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


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


def settings(**overrides: str) -> Settings:
    values: dict[str, Any] = {**_MINIMAL, **overrides}
    return Settings(_env_file=None, **values)


def test_default_configuration_resolves_to_the_fake() -> None:
    assert isinstance(get_llm(settings=settings()), FakeLLM)


def test_caller_only_sees_the_abstraction() -> None:
    assert isinstance(get_llm(settings=settings()), LLMProvider)


def test_fake_is_registered() -> None:
    assert "fake" in available_providers()


def test_unknown_provider_names_the_typo_and_the_alternatives() -> None:
    with pytest.raises(ValueError, match="unknown llm provider 'openai'"):
        get_llm(settings=settings(LLM_PROVIDER="openai"))


def test_error_message_lists_what_is_implemented() -> None:
    with pytest.raises(ValueError, match=r"implemented: \['fake'\]"):
        build_llm("nope")


def test_each_call_gets_its_own_instance() -> None:
    """Providers carry per-session state — FakeLLM's prompt cache — and sharing
    it would make a cache hit depend on which caller ran first."""
    configured = settings()
    assert get_llm(settings=configured) is not get_llm(settings=configured)
