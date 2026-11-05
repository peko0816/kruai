"""Azure neural TTS behind the normative ``TTSProvider`` (BACKLOG E8).

Moved from m0/ ahead of M0-2's verdict, with the owner's approval (D-137): the
trial needs real audio before the native speakers have scored the voices.

Khmer has exactly two Azure voices, ``km-KH-SreymomNeural`` and
``km-KH-PisethNeural`` (both GA in eastasia, checked against the live voice
list on 2026-10-24). Which one reads what is configuration —
TTS_VOICE_KM_NARRATOR, TTS_VOICE_KM_FEEDBACK, TTS_VOICE_ZH_MODEL — so the
blind test's verdict is a settings change and a rebuild, not a code change.
The constructor still takes a mapping, because the blind test builds this
class once per candidate voice.

Build time only, like every TTS provider (tts/base.py): nothing in a request
path may call it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Final
from xml.sax.saxutils import escape, quoteattr

import httpx

from app.core.config import Settings
from app.services.azure_speech import Sleep, post
from app.services.provider_errors import ProviderConfigurationError
from app.services.tts.base import SynthesisResult, TTSProvider, Voice

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


#: Azure takes at most fifty voice elements in one SSML document.
MAX_VOICE_PIECES: Final = 50


def ssml_pieces(pieces: Sequence[tuple[str, str, float]]) -> str:
    """One document, one ``<voice>`` per (vendor voice, text, rate) piece.

    The document's language is the first voice's; each voice speaks its own.
    """
    language = "-".join(pieces[0][0].split("-")[:2])
    body = "".join(
        f"<voice name={quoteattr(vendor_voice)}>"
        f"<prosody rate={quoteattr(f'{round((rate - 1) * 100):+d}%')}>{escape(text)}</prosody>"
        "</voice>"
        for vendor_voice, text, rate in pieces
    )
    return f"<speak version='1.0' xml:lang={quoteattr(language)}>{body}</speak>"


class AzureTTS(TTSProvider):
    """Synthesises through Azure's REST endpoint. Never raises from synthesize."""

    name = "azure"

    def __init__(
        self,
        *,
        key: str,
        region: str,
        voices: Mapping[Voice, str],
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
        self._voices = dict(voices)
        self._transport = transport
        self._sleep = sleep

    @classmethod
    def from_settings(
        cls, settings: Settings, *, voices: Mapping[Voice, str] | None = None
    ) -> AzureTTS:
        """The configured voices, with ``voices`` overriding any of them.

        EN_MODEL is v2 and has no setting, which is how base.py says a voice
        is not offered.
        """
        configured = {
            Voice.KM_NARRATOR: settings.tts_voice_km_narrator,
            Voice.KM_FEEDBACK: settings.tts_voice_km_feedback,
            Voice.ZH_MODEL: settings.tts_voice_zh_model,
        }
        return cls(
            key=settings.azure_speech_key.get_secret_value(),
            region=settings.azure_speech_region,
            voices={**configured, **(voices or {})},
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

    async def synthesize_pieces(
        self, pieces: Sequence[tuple[Voice, str, float]], *, audio_format: str = "mp3"
    ) -> SynthesisResult:
        """Several voices in one request, as one recording (D-139).

        Not part of the normative interface: it is the optional capability
        ``pipeline.build_pack`` looks for when a Khmer explanation quotes
        Chinese. Asked of Azure as one SSML document, the voice changes come
        back as ordinary pauses; synthesised piece by piece and joined, each
        piece carried its own lead-in and tail, and an explanation that switches
        twenty times sounded chopped up.
        """
        if not pieces or not any(text.strip() for _, text, _ in pieces):
            return _failure("tts.empty_text", "nothing to synthesise")
        if len(pieces) > MAX_VOICE_PIECES:
            return _failure(
                "tts.too_many_voices", f"{len(pieces)} pieces; azure takes {MAX_VOICE_PIECES}"
            )
        if audio_format != "mp3":
            return _failure(
                "tts.unsupported_format", f"{self.name} emits mp3 only, asked for {audio_format!r}"
            )
        resolved: list[tuple[str, str, float]] = []
        for voice, text, rate in pieces:
            vendor_voice = self._voices.get(voice)
            if vendor_voice is None:
                return _failure("tts.unsupported_voice", f"no azure voice for {voice.value}")
            if not _MIN_RATE <= rate <= _MAX_RATE:
                return _failure(
                    "tts.invalid_speaking_rate",
                    f"speaking_rate must be within {_MIN_RATE}-{_MAX_RATE}, got {rate}",
                )
            resolved.append((vendor_voice, text, rate))

        outcome = await post(
            self._url,
            headers={
                "Ocp-Apim-Subscription-Key": self._key,
                "Content-Type": "application/ssml+xml",
                "X-Microsoft-OutputFormat": _OUTPUT_FORMAT,
                "User-Agent": "kruai-m0",
            },
            content=ssml_pieces(resolved).encode(),
            timeout_seconds=_TIMEOUT_SECONDS,
            transport=self._transport,
            sleep=self._sleep,
        )
        cost = sum(len(text) for _, text, _ in resolved) // _CHARS_PER_USD_CENT
        if outcome.response is None:
            billed = cost if outcome.error == "timeout" else 0
            return _failure(f"tts.{outcome.error}", outcome.message or "", cost=billed)
        audio = outcome.response.content
        if not audio:
            return _failure("tts.empty_audio", "azure answered 200 with no audio", cost=cost)
        voices = list(dict.fromkeys(vendor_voice for vendor_voice, _, _ in resolved))
        return SynthesisResult(
            ok=True,
            audio=audio,
            audio_format="mp3",
            duration_ms=len(audio) * 8 // _BITS_PER_MS,
            resolved_voice="+".join(voices),
            cost_usd_cents=cost,
        )


def _failure(code: str, message: str, *, cost: int = 0) -> SynthesisResult:
    return SynthesisResult(ok=False, error_code=code, error_message=message, cost_usd_cents=cost)


__all__ = ["MAX_VOICE_PIECES", "AzureTTS", "ssml", "ssml_pieces"]
