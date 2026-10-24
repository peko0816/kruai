"""Azure neural TTS behind the normative ``TTSProvider``, for M0-2.

Khmer has exactly two Azure voices, ``km-KH-SreymomNeural`` and
``km-KH-PisethNeural`` (both GA in eastasia, checked against the live voice
list on 2026-10-24). The blind test exists to choose between them, so the
mapping from logical voice to vendor voice is a constructor argument rather
than a constant: the same class is built once per candidate.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Final
from xml.sax.saxutils import escape, quoteattr

import httpx

from app.core.config import Settings
from app.services.provider_errors import ProviderConfigurationError
from app.services.tts.base import SynthesisResult, TTSProvider, Voice
from m0.azure.transport import Sleep, post

#: What a logical voice resolves to when nobody pinned one. Khmer feedback
#: shares the narrator until M0-2 says otherwise (base.py allows "same
#: source"). EN_MODEL is v2 and is left out, which is how base.py says a
#: voice is not offered.
DEFAULT_VOICES: Final[Mapping[Voice, str]] = {
    Voice.KM_NARRATOR: "km-KH-SreymomNeural",
    Voice.KM_FEEDBACK: "km-KH-SreymomNeural",
    Voice.ZH_MODEL: "zh-CN-XiaoxiaoNeural",
}

#: Constant bit rate, so the duration can be read off the byte count: at
#: 48 kbit/s every millisecond is 48 bits. The same reasoning as FakeTTS's
#: frame arithmetic — a stated duration that disagrees with the audio is a bug
#: nothing catches until someone listens.
_OUTPUT_FORMAT: Final = "audio-24khz-48kbitrate-mono-mp3"
_BITS_PER_MS: Final = 48

#: Azure lists neural TTS at $15 per million characters. Floored per call, as
#: FakeTTS does and for its reason: a sentence costs a small fraction of a
#: cent, and rounding each up to one would make a pack build look like a
#: hundred dollars. The free tier bills nothing at all; this is the figure
#: the ledger would need if the resource were upgraded.
_CHARS_PER_USD_CENT: Final = 666

#: SSML prosody accepts -50% to +100%.
_MIN_RATE: Final = 0.5
_MAX_RATE: Final = 2.0

#: A long Khmer sentence takes a couple of seconds to synthesise; thirty is
#: slack for a throttled free tier, not an expectation.
_TIMEOUT_SECONDS: Final = 30.0


def ssml(text: str, *, vendor_voice: str, speaking_rate: float) -> str:
    """The request body. The language comes from the voice name (km-KH-...)."""
    language = "-".join(vendor_voice.split("-")[:2])
    rate = f"{round((speaking_rate - 1) * 100):+d}%"
    return (
        f"<speak version='1.0' xml:lang={quoteattr(language)}>"
        f"<voice name={quoteattr(vendor_voice)}>"
        f"<prosody rate={quoteattr(rate)}>{escape(text)}</prosody>"
        "</voice></speak>"
    )


class AzureTTS(TTSProvider):
    """Synthesises through Azure's REST endpoint. Never raises from synthesize."""

    name = "azure"

    def __init__(
        self,
        *,
        key: str,
        region: str,
        voices: Mapping[Voice, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        """
        Raises:
            ProviderConfigurationError: no key or no region. Raised here, at
                construction, so a script stops before its first request
                rather than reporting thirty identical auth failures.
        """
        if not key or not region:
            raise ProviderConfigurationError(
                "azure tts needs AZURE_SPEECH_KEY and AZURE_SPEECH_REGION in .env"
            )
        self._key = key
        self._url = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"
        self._voices = {**DEFAULT_VOICES, **(voices or {})}
        self._transport = transport
        self._sleep = sleep

    @classmethod
    def from_settings(
        cls, settings: Settings, *, voices: Mapping[Voice, str] | None = None
    ) -> AzureTTS:
        return cls(
            key=settings.azure_speech_key.get_secret_value(),
            region=settings.azure_speech_region,
            voices=voices,
        )

    def available_voices(self) -> dict[Voice, str]:
        return dict(self._voices)

    async def synthesize(
        self,
        text: str,
        *,
        voice: Voice,
        speaking_rate: float = 1.0,
        audio_format: str = "mp3",
    ) -> SynthesisResult:
        if not text.strip():
            return _failure("tts.empty_text", "nothing to synthesise")
        if not _MIN_RATE <= speaking_rate <= _MAX_RATE:
            return _failure(
                "tts.invalid_speaking_rate",
                f"speaking_rate must be within {_MIN_RATE}-{_MAX_RATE}, got {speaking_rate}",
            )
        if audio_format != "mp3":
            return _failure(
                "tts.unsupported_format", f"{self.name} emits mp3 only, asked for {audio_format!r}"
            )
        vendor_voice = self._voices.get(voice)
        if vendor_voice is None:
            return _failure("tts.unsupported_voice", f"no azure voice for {voice.value}")

        outcome = await post(
            self._url,
            headers={
                "Ocp-Apim-Subscription-Key": self._key,
                "Content-Type": "application/ssml+xml",
                "X-Microsoft-OutputFormat": _OUTPUT_FORMAT,
                "User-Agent": "kruai-m0",
            },
            content=ssml(text, vendor_voice=vendor_voice, speaking_rate=speaking_rate).encode(),
            timeout_seconds=_TIMEOUT_SECONDS,
            transport=self._transport,
            sleep=self._sleep,
        )
        cost = len(text) // _CHARS_PER_USD_CENT
        if outcome.response is None:
            # A timeout may have been synthesised and billed; a refusal was not.
            billed = cost if outcome.error == "timeout" else 0
            return _failure(f"tts.{outcome.error}", outcome.message or "", cost=billed)

        audio = outcome.response.content
        if not audio:
            return _failure("tts.empty_audio", "azure answered 200 with no audio", cost=cost)
        return SynthesisResult(
            ok=True,
            audio=audio,
            audio_format="mp3",
            duration_ms=len(audio) * 8 // _BITS_PER_MS,
            resolved_voice=vendor_voice,
            cost_usd_cents=cost,
        )


def _failure(code: str, message: str, *, cost: int = 0) -> SynthesisResult:
    return SynthesisResult(ok=False, error_code=code, error_message=message, cost_usd_cents=cost)


__all__ = ["DEFAULT_VOICES", "AzureTTS", "ssml"]
