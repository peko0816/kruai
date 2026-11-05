"""TTS routing is by voice, not language.

That is the axis M0-2 may split on: if Azure has no km-KH voice, Khmer narration
and Chinese demonstration come from different vendors, and the registry has to
be able to say so rather than quietly substituting.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.config import Settings
from app.services.provider_errors import ProviderConfigurationError
from app.services.tts import get_tts, speaking_rate_for
from app.services.tts import registry as tts_registry
from app.services.tts.base import SynthesisResult, TTSProvider, Voice
from app.services.tts.fake import FakeTTS
from app.services.tts.registry import available_providers, build_tts

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


class ChineseOnlyTTS(TTSProvider):
    """The shape M0-2 is worried about: a vendor with no Khmer voice."""

    name = "zh-only"

    def available_voices(self) -> dict[Voice, str]:
        return {Voice.ZH_MODEL: "vendor-zh-female-1"}

    async def synthesize(
        self,
        text: str,
        *,
        voice: Voice,
        speaking_rate: float = 1.0,
        audio_format: str = "mp3",
    ) -> SynthesisResult:
        raise AssertionError("routing tests never synthesise")


@pytest.fixture
def zh_only_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(tts_registry._FACTORIES, "zh-only", lambda _s, _v: ChineseOnlyTTS())


# ------------------------------------------------------------- default wiring


@pytest.mark.parametrize("voice", list(Voice))
def test_default_configuration_covers_every_voice(voice: Voice) -> None:
    """Gate G-B: an all-fake pack build must never stall on a missing voice."""
    assert isinstance(get_tts(voice, settings=settings()), FakeTTS)


def test_caller_only_sees_the_abstraction() -> None:
    assert isinstance(get_tts(Voice.KM_NARRATOR, settings=settings()), TTSProvider)


def test_fake_is_registered() -> None:
    assert "fake" in available_providers()


# --------------------------------------------------------------- misconfigured


def test_unknown_provider_names_the_typo_and_the_alternatives() -> None:
    """ElevenLabs has no Khmer TTS and is not implemented (D-125)."""
    with pytest.raises(ValueError, match="unknown tts provider 'elevenlabs'"):
        get_tts(Voice.ZH_MODEL, settings=settings(TTS_PROVIDER="elevenlabs"))


def test_error_message_lists_what_is_implemented() -> None:
    with pytest.raises(ValueError, match=r"implemented: \['azure', 'fake'\]"):
        build_tts("nope", settings=settings())


@pytest.mark.usefixtures("zh_only_registered")
def test_missing_voice_fails_rather_than_substituting() -> None:
    """Falling back to another voice would ship a pack whose Khmer explanation
    is read in Chinese — worse than a build that stops."""
    configured = settings(TTS_PROVIDER="zh-only")

    assert isinstance(get_tts(Voice.ZH_MODEL, settings=configured), ChineseOnlyTTS)
    with pytest.raises(ValueError, match="has no voice for km_narrator"):
        get_tts(Voice.KM_NARRATOR, settings=configured)


@pytest.mark.usefixtures("zh_only_registered")
def test_missing_voice_error_lists_what_is_offered() -> None:
    with pytest.raises(ValueError, match=r"it offers \['zh_model'\]"):
        get_tts(Voice.EN_MODEL, settings=settings(TTS_PROVIDER="zh-only"))


# -------------------------------------------------------------- speaking rate


def test_chinese_model_uses_the_configured_slow_rate() -> None:
    """PRD 6.3: the demonstration is slowed because learners copy it."""
    assert speaking_rate_for(Voice.ZH_MODEL, settings=settings()) == 0.9


@pytest.mark.parametrize("voice", [Voice.KM_NARRATOR, Voice.KM_FEEDBACK])
def test_khmer_voices_use_the_khmer_rate(voice: Voice) -> None:
    assert speaking_rate_for(voice, settings=settings()) == 1.0


def test_rates_come_from_configuration_not_constants() -> None:
    configured = settings(TTS_SPEAKING_RATE_ZH="0.75", TTS_SPEAKING_RATE_KM="1.1")
    assert speaking_rate_for(Voice.ZH_MODEL, settings=configured) == 0.75
    assert speaking_rate_for(Voice.KM_NARRATOR, settings=configured) == 1.1


# ------------------------------------------------------------------- azure (E8)


def test_azure_is_registered_and_reads_its_voices_from_settings() -> None:
    """The blind test's verdict is a settings change, not a code change (D-137)."""
    from app.services.tts.azure import AzureTTS

    configured = settings(
        TTS_PROVIDER="azure",
        AZURE_SPEECH_KEY="k",
        AZURE_SPEECH_REGION="eastasia",
        TTS_VOICE_KM_NARRATOR="km-KH-PisethNeural",
    )

    provider = get_tts(Voice.KM_NARRATOR, settings=configured)

    assert isinstance(provider, AzureTTS)
    assert provider.available_voices()[Voice.KM_NARRATOR] == "km-KH-PisethNeural"
    assert provider.available_voices()[Voice.ZH_MODEL] == "zh-CN-XiaoxiaoNeural"


def test_a_pinned_voice_overrides_the_configured_one() -> None:
    """How the blind test builds one provider per candidate."""
    configured = settings(AZURE_SPEECH_KEY="k", AZURE_SPEECH_REGION="eastasia")

    provider = build_tts(
        "azure", settings=configured, voices={Voice.KM_NARRATOR: "km-KH-PisethNeural"}
    )

    assert provider.available_voices()[Voice.KM_NARRATOR] == "km-KH-PisethNeural"
    assert provider.available_voices()[Voice.KM_FEEDBACK] == "km-KH-SreymomNeural"


def test_azure_without_a_key_is_a_configuration_error() -> None:
    """So the startup self-check refuses to boot rather than a pack build failing later."""
    with pytest.raises(ProviderConfigurationError, match="AZURE_SPEECH_KEY"):
        get_tts(Voice.KM_NARRATOR, settings=settings(TTS_PROVIDER="azure"))


def test_azure_offers_no_english_voice_yet() -> None:
    """EN_MODEL is v2; an English pack build stops at once rather than reading in Khmer."""
    configured = settings(TTS_PROVIDER="azure", AZURE_SPEECH_KEY="k", AZURE_SPEECH_REGION="r")

    with pytest.raises(ProviderConfigurationError, match="en_model"):
        get_tts(Voice.EN_MODEL, settings=configured)
