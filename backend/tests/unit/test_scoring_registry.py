"""The registry is the seam M0 swaps a vendor through.

What matters is that callers get something implementing the base class without
naming a vendor, and that a deployment which cannot score a needed language
fails loudly rather than at the first learner's recording.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.config import Settings
from app.services.provider_errors import ProviderConfigurationError
from app.services.scoring import get_scorer
from app.services.scoring import registry as scoring_registry
from app.services.scoring.base import (
    AssessMode,
    Language,
    PronunciationResult,
    PronunciationScorer,
)
from app.services.scoring.fake import FakeScorer
from app.services.scoring.registry import (
    available_providers,
    build_scorer,
    provider_chain,
)

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


class ZhOnlyScorer(PronunciationScorer):
    """Stand-in for a vendor with partial coverage, which is the realistic case."""

    name = "zh-only"

    def supports(self, language: Language) -> bool:
        return language is Language.ZH_CN

    async def assess(
        self,
        audio: bytes,
        *,
        language: Language,
        mode: AssessMode,
        reference_text: str | None = None,
        audio_format: str = "wav",
    ) -> PronunciationResult:
        raise AssertionError("routing tests never assess")


@pytest.fixture
def zh_only_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register a partial-coverage provider for the duration of one test."""
    monkeypatch.setitem(scoring_registry._FACTORIES, "zh-only", lambda _s: ZhOnlyScorer())


# ------------------------------------------------------------- default wiring


@pytest.mark.parametrize("language", list(Language))
def test_default_configuration_resolves_every_language(language: Language) -> None:
    """Gate G-B: all-fake has to work end to end, for any language."""
    assert isinstance(get_scorer(language, settings=settings()), FakeScorer)


def test_caller_only_sees_the_abstraction() -> None:
    scorer = get_scorer(Language.ZH_CN, settings=settings())
    assert isinstance(scorer, PronunciationScorer)


def test_fake_is_registered() -> None:
    assert "fake" in available_providers()


def test_build_scorer_reports_its_ledger_name() -> None:
    assert build_scorer("fake", settings=settings()).name == "fake"


# --------------------------------------------------------------- misconfigured


def test_unknown_provider_names_the_typo_and_the_alternatives() -> None:
    with pytest.raises(ValueError, match="unknown scoring provider 'iflytek'"):
        get_scorer(Language.ZH_CN, settings=settings(SCORING_PROVIDER="iflytek"))


def test_unknown_fallback_is_caught_too() -> None:
    """A fallback only runs when the primary misses, so typos hide there longest."""
    configured = settings(SCORING_PROVIDER="fake", SCORING_FALLBACK_PROVIDERS="typo")
    # fake covers everything, so the broken fallback is never reached...
    assert get_scorer(Language.ZH_CN, settings=configured) is not None
    # ...but building it directly still fails loudly, which is what B5 will sweep.
    with pytest.raises(ValueError, match="unknown scoring provider 'typo'"):
        build_scorer("typo", settings=configured)


def test_error_message_lists_what_is_implemented() -> None:
    with pytest.raises(ValueError, match=r"implemented: \['azure', 'fake'\]"):
        build_scorer("nope", settings=settings())


# ------------------------------------------------------------- fallback chain


def test_chain_is_primary_then_fallbacks_in_order() -> None:
    configured = settings(SCORING_PROVIDER="fake", SCORING_FALLBACK_PROVIDERS="a,b")
    assert provider_chain(configured) == ("fake", "a", "b")


def test_chain_is_just_the_primary_when_no_fallbacks() -> None:
    assert provider_chain(settings()) == ("fake",)


@pytest.mark.usefixtures("zh_only_registered")
def test_fallback_is_used_when_the_primary_cannot_cover_the_language() -> None:
    configured = settings(SCORING_PROVIDER="zh-only", SCORING_FALLBACK_PROVIDERS="fake")

    assert isinstance(get_scorer(Language.ZH_CN, settings=configured), ZhOnlyScorer)
    # en-US falls through to the one that can handle it.
    assert isinstance(get_scorer(Language.EN_US, settings=configured), FakeScorer)


@pytest.mark.usefixtures("zh_only_registered")
def test_language_nobody_supports_names_the_language() -> None:
    """B5 turns this into a boot-time sweep; the message is what it will surface."""
    configured = settings(SCORING_PROVIDER="zh-only")

    with pytest.raises(ValueError, match="no configured scoring provider supports en-US"):
        get_scorer(Language.EN_US, settings=configured)


# ------------------------------------------------------------------- azure (E9)


def test_azure_is_registered_and_reads_its_settings() -> None:
    from app.services.scoring.azure import AzureScorer

    configured = settings(
        SCORING_PROVIDER="azure", AZURE_SPEECH_KEY="k", AZURE_SPEECH_REGION="eastasia"
    )

    assert isinstance(get_scorer(Language.ZH_CN, settings=configured), AzureScorer)


async def test_azure_bills_at_the_configured_price(monkeypatch: pytest.MonkeyPatch) -> None:
    """The owner sets cost caps from pricing, so the estimate is configuration too."""
    import io
    import wave

    from app.services.azure_speech import Outcome

    async def timed_out(*_: Any, **__: Any) -> Outcome:
        return Outcome(error="timeout", message="")  # billed: the audio was sent

    monkeypatch.setattr("app.services.scoring.azure.post", timed_out)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16_000)
        writer.writeframes(bytes(36 * 16_000 * 2))
    configured = settings(
        SCORING_PROVIDER="azure",
        AZURE_SPEECH_KEY="k",
        AZURE_SPEECH_REGION="eastasia",
        SCORING_PRICE_USD_CENTS_PER_HOUR="100",
    )

    result = await get_scorer(Language.ZH_CN, settings=configured).assess(
        buffer.getvalue(), language=Language.ZH_CN, mode=AssessMode.SCRIPTED, reference_text="x"
    )

    assert result.cost_usd_millicents == 1000, "36 s at $1 an hour is one cent"


def test_azure_without_a_key_is_a_configuration_error() -> None:
    """So the startup self-check refuses to boot rather than the first learner failing."""
    with pytest.raises(ProviderConfigurationError, match="AZURE_SPEECH_KEY"):
        get_scorer(Language.ZH_CN, settings=settings(SCORING_PROVIDER="azure"))


def test_azure_with_parse_tone_mode_is_a_configuration_error() -> None:
    configured = settings(
        SCORING_PROVIDER="azure",
        AZURE_SPEECH_KEY="k",
        AZURE_SPEECH_REGION="eastasia",
        ZH_TONE_MODE="parse",
    )

    with pytest.raises(ProviderConfigurationError, match="D-140"):
        get_scorer(Language.ZH_CN, settings=configured)
