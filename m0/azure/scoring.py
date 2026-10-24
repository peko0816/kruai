"""Azure pronunciation assessment behind the normative ``PronunciationScorer``, for M0-1.

Uses the short-audio REST endpoint with a ``Pronunciation-Assessment`` header.
Two things about it shape this module:

* **It takes 16 kHz mono 16-bit WAV, or Ogg Opus, up to 60 seconds.** Phone
  recordings are usually 44.1 or 48 kHz, so the common failure is a format
  refusal, and it is caught here — before the request, where it costs nothing
  — with the ffmpeg line that fixes it.
* **The response has not been seen yet.** Microsoft's REST examples put the
  scores directly on each node; the SDK nests them under
  ``PronunciationAssessment``. Both shapes are read, and anything else comes
  back as ``scoring.malformed_response`` with the body kept in ``raw``, so
  the first real run says what it got instead of a KeyError.

``tone_score`` is always None: whether tone can be parsed from the phonemes
or must be derived is exactly what M0-1 is run to find out (ZH_TONE_MODE).
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import math
import wave
from collections.abc import Mapping
from typing import Any, Final

import httpx

from app.core.config import Settings
from app.services.provider_errors import ProviderConfigurationError
from app.services.scoring.base import (
    AssessMode,
    Language,
    PhonemeScore,
    PronunciationResult,
    PronunciationScorer,
    WordScore,
)
from m0.azure.transport import Sleep, post

_SAMPLE_RATE: Final = 16_000
_MAX_SECONDS: Final = 60.0
_FFMPEG_HINT: Final = "convert with: ffmpeg -i IN -ar 16000 -ac 1 -sample_fmt s16 OUT.wav"

#: Speech-to-text is listed at $1 an hour and assessment is billed on top of
#: it; the add-on's US price could not be confirmed (Azure China lists it per
#: hour per feature). $2 an hour is the upper bound assumed until a bill says
#: otherwise, and each call is rounded up — base.py asks for a conservative
#: overestimate when the real figure is unknown. The free tier bills nothing.
_USD_CENTS_PER_HOUR: Final = 200

#: Azure reports offsets and durations in 100-nanosecond ticks.
_TICKS_PER_MS: Final = 10_000

#: RecognitionStatus values that mean the audio had no usable speech, as
#: opposed to the service failing.
_NO_SPEECH: Final = frozenset({"NoMatch", "InitialSilenceTimeout", "BabbleTimeout"})


class AzureScorer(PronunciationScorer):
    """Scripted assessment through Azure's REST endpoint. Never raises from assess."""

    name = "azure"

    def __init__(
        self,
        *,
        key: str,
        region: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        """
        Raises:
            ProviderConfigurationError: no key or no region — raised before
                the first recording is sent, not once per recording.
        """
        if not key or not region:
            raise ProviderConfigurationError(
                "azure scoring needs AZURE_SPEECH_KEY and AZURE_SPEECH_REGION in .env"
            )
        self._key = key
        self._url = (
            f"https://{region}.stt.speech.microsoft.com"
            "/speech/recognition/conversation/cognitiveservices/v1"
        )
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._sleep = sleep

    @classmethod
    def from_settings(cls, settings: Settings) -> AzureScorer:
        return cls(
            key=settings.azure_speech_key.get_secret_value(),
            region=settings.azure_speech_region,
            timeout_seconds=settings.scoring_timeout_ms / 1000,
        )

    def supports(self, language: Language) -> bool:
        return language in (Language.ZH_CN, Language.EN_US)

    async def assess(
        self,
        audio: bytes,
        *,
        language: Language,
        mode: AssessMode,
        reference_text: str | None = None,
        audio_format: str = "wav",
    ) -> PronunciationResult:
        # M0-1 is scripted throughout. Unscripted assessment on this endpoint
        # is unverified, and a verification adapter guessing at it would
        # produce numbers nobody asked for.
        if mode is not AssessMode.SCRIPTED:
            return _failure("scoring.unsupported_mode", "the m0 adapter assesses scripted only")
        if not reference_text:
            return _failure("scoring.missing_reference", "scripted assessment needs reference_text")

        prepared = inspect_audio(audio, audio_format)
        if isinstance(prepared, PronunciationResult):
            return prepared
        content_type, seconds = prepared
        cost = estimate_cost(seconds)

        outcome = await post(
            f"{self._url}?language={language.value}&format=detailed",
            headers={
                "Ocp-Apim-Subscription-Key": self._key,
                "Content-Type": content_type,
                "Accept": "application/json",
                "Pronunciation-Assessment": assessment_header(reference_text, language),
            },
            content=audio,
            timeout_seconds=self._timeout_seconds,
            transport=self._transport,
            sleep=self._sleep,
        )
        if outcome.response is None:
            billed = cost if outcome.error == "timeout" else 0
            return _failure(f"scoring.{outcome.error}", outcome.message or "", cost=billed)

        try:
            body = outcome.response.json()
        except ValueError:
            return _failure("scoring.malformed_response", "response was not JSON", cost=cost)
        return parse(body, language=language, cost=cost)


def assessment_header(reference_text: str, language: Language) -> str:
    """Base64 JSON, as the endpoint requires. Prosody exists for en-US only."""
    params: dict[str, Any] = {
        "ReferenceText": reference_text,
        "GradingSystem": "HundredMark",
        "Granularity": "Phoneme",
        "Dimension": "Comprehensive",
        "EnableMiscue": True,
    }
    if language is Language.EN_US:
        params["EnableProsodyAssessment"] = True
    return base64.b64encode(json.dumps(params, ensure_ascii=False).encode()).decode("ascii")


def inspect_audio(
    audio: bytes, audio_format: str
) -> tuple[str, float | None] | PronunciationResult:
    """The Content-Type to send and the length to bill, or why it cannot be sent."""
    if audio_format == "ogg":
        # Opus carries no cheap length; billing assumes the longest allowed.
        return "audio/ogg; codecs=opus", None
    if audio_format != "wav":
        return _failure(
            "scoring.unsupported_format",
            f"azure takes wav or ogg, not {audio_format}; {_FFMPEG_HINT}",
        )
    try:
        with wave.open(io.BytesIO(audio)) as reader:
            channels = reader.getnchannels()
            width = reader.getsampwidth()
            rate = reader.getframerate()
            seconds = reader.getnframes() / rate
    except (wave.Error, EOFError, ZeroDivisionError):
        return _failure("scoring.unsupported_audio", f"not a readable PCM wav; {_FFMPEG_HINT}")
    if (channels, width, rate) != (1, 2, _SAMPLE_RATE):
        return _failure(
            "scoring.unsupported_audio",
            f"wav is {channels}ch/{width * 8}bit/{rate}Hz, azure needs 1ch/16bit/16000Hz; "
            f"{_FFMPEG_HINT}",
        )
    if seconds > _MAX_SECONDS:
        return _failure(
            "scoring.audio_too_long", f"{seconds:.1f}s; the endpoint takes {_MAX_SECONDS:.0f}s"
        )
    return f"audio/wav; codecs=audio/pcm; samplerate={_SAMPLE_RATE}", seconds


def estimate_cost(seconds: float | None) -> int:
    """US cents, rounded up. Unknown length is billed as the maximum."""
    length = _MAX_SECONDS if seconds is None else seconds
    return math.ceil(length * _USD_CENTS_PER_HOUR / 3600)


def parse(body: Mapping[str, Any], *, language: Language, cost: int) -> PronunciationResult:
    """Turn a detailed-format response into the interface's result."""
    raw = dict(body)
    status = body.get("RecognitionStatus")
    if status != "Success":
        code = "scoring.no_speech" if status in _NO_SPEECH else "scoring.recognition_failed"
        return _failure(code, f"RecognitionStatus={status}", cost=cost, raw=raw)
    try:
        best = body["NBest"][0]
        scores = _assessment(best)
        pron = _score(scores, "PronScore")
        if pron is None:
            return _failure(
                "scoring.no_assessment",
                "recognised but not assessed; was the Pronunciation-Assessment header read?",
                cost=cost,
                raw=raw,
            )
        words = tuple(_word(w) for w in best.get("Words", ()))
        return PronunciationResult(
            ok=True,
            asr_text=str(best.get("Display") or body.get("DisplayText") or ""),
            pron_score=pron,
            accuracy_score=_score(scores, "AccuracyScore"),
            fluency_score=_score(scores, "FluencyScore"),
            completeness_score=_score(scores, "CompletenessScore"),
            prosody_score=_score(scores, "ProsodyScore") if language is Language.EN_US else None,
            tone_score=None,
            words=words,
            raw=raw,
            cost_usd_cents=cost,
        )
    except (KeyError, IndexError, TypeError, ValueError) as error:
        return _failure(
            "scoring.malformed_response", f"{type(error).__name__}: {error}", cost=cost, raw=raw
        )


def _assessment(node: Mapping[str, Any]) -> Mapping[str, Any]:
    """REST puts scores on the node itself; the SDK nests them. Read either."""
    nested = node.get("PronunciationAssessment")
    return nested if isinstance(nested, Mapping) else node


def _score(scores: Mapping[str, Any], key: str) -> float | None:
    value = scores.get(key)
    return None if value is None else float(value)


def _ms(ticks: Any) -> int | None:
    return None if ticks is None else int(ticks) // _TICKS_PER_MS


def _word(node: Mapping[str, Any]) -> WordScore:
    scores = _assessment(node)
    error_type = scores.get("ErrorType")
    return WordScore(
        word=str(node["Word"]),
        # An omitted word may carry no accuracy at all; it was not said.
        accuracy=_score(scores, "AccuracyScore") or 0.0,
        error_type=None if error_type in (None, "None") else str(error_type),
        phonemes=tuple(
            PhonemeScore(
                phoneme=str(p["Phoneme"]),
                accuracy=float(_assessment(p)["AccuracyScore"]),
                offset_ms=_ms(p.get("Offset")),
                duration_ms=_ms(p.get("Duration")),
            )
            for p in node.get("Phonemes", ())
        ),
    )


def _failure(
    code: str, message: str, *, cost: int = 0, raw: dict[str, Any] | None = None
) -> PronunciationResult:
    return PronunciationResult(
        ok=False, error_code=code, error_message=message, cost_usd_cents=cost, raw=raw or {}
    )


__all__ = ["AzureScorer", "assessment_header", "estimate_cost", "inspect_audio", "parse"]
