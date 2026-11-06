"""Azure pronunciation assessment behind the normative ``PronunciationScorer`` (BACKLOG E9).

Moved from m0/ ahead of M0-1's verdict, with the owner's approval (D-141).

Uses the short-audio REST endpoint with a ``Pronunciation-Assessment`` header.
Two things about it shape this module:

* **It takes 16 kHz mono 16-bit WAV, or Ogg Opus, up to 60 seconds.** Phone
  recordings are usually 44.1 or 48 kHz, so the common failure is a format
  refusal, and it is caught here — before the request, where it costs nothing
  — with the ffmpeg line that fixes it. Both formats are billed by their real
  length, read from the file (Ogg Opus: ``scoring/ogg.py``).
* **The response shape.** The REST endpoint puts the scores directly on each
  node (seen on 2026-11-05); the SDK nests them under ``PronunciationAssessment``.
  Both shapes are read, and anything else comes back as
  ``scoring.malformed_response`` with the body kept in ``raw``.

Scripted when there is a reference sentence; unscripted otherwise, which
Azure answers by recognising what was said and assessing that (tried for real
on 2026-11-05). Unscripted reports no completeness: there was nothing to be
complete against (scoring/base.py).

Mandarin tone comes from ``scoring/zh_tone.py``, derived from syllable accuracy
(D-140).
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
from app.services.azure_speech import Sleep, post
from app.services.provider_errors import ProviderConfigurationError
from app.services.scoring.base import (
    AssessMode,
    Language,
    PhonemeScore,
    PronunciationResult,
    PronunciationScorer,
    WordScore,
)
from app.services.scoring.ogg import opus_seconds
from app.services.scoring.zh_tone import ToneMode, check_tone_mode, tone_score

_SAMPLE_RATE: Final = 16_000
_MAX_SECONDS: Final = 60.0
_FFMPEG_HINT: Final = "convert with: ffmpeg -i IN -ar 16000 -ac 1 -sample_fmt s16 OUT.wav"

_MILLICENTS_PER_CENT: Final = 1000

#: Azure reports offsets and durations in 100-nanosecond ticks.
_TICKS_PER_MS: Final = 10_000

#: RecognitionStatus values that mean the audio had no usable speech, as
#: opposed to the service failing.
_NO_SPEECH: Final = frozenset({"NoMatch", "InitialSilenceTimeout", "BabbleTimeout"})


class AzureScorer(PronunciationScorer):
    """Assessment through Azure's REST endpoint. Never raises from assess."""

    name = "azure"

    def __init__(
        self,
        *,
        key: str,
        region: str,
        timeout_seconds: float,
        usd_cents_per_hour: int,
        tone_mode: ToneMode = "derive",
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        """
        Raises:
            ProviderConfigurationError: no key or no region, or a tone mode
                that cannot work — raised before the first recording is sent,
                so the startup self-check refuses to boot.
        """
        if not key or not region:
            raise ProviderConfigurationError(
                "azure scoring needs AZURE_SPEECH_KEY and AZURE_SPEECH_REGION in .env"
            )
        check_tone_mode(tone_mode)
        self._usd_cents_per_hour = usd_cents_per_hour
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
            usd_cents_per_hour=settings.scoring_price_usd_cents_per_hour,
            tone_mode=settings.zh_tone_mode,
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
        scripted = mode is AssessMode.SCRIPTED
        if scripted and not reference_text:
            return _failure("scoring.missing_reference", "scripted assessment needs reference_text")
        if not scripted and reference_text is not None:
            # base.py: UNSCRIPTED takes no reference. Honouring one would be a
            # scripted assessment the caller did not ask for.
            return _failure("scoring.reference_not_allowed", "unscripted takes no reference_text")

        prepared = inspect_audio(audio, audio_format)
        if isinstance(prepared, PronunciationResult):
            return prepared
        content_type, seconds = prepared
        cost = estimate_cost(seconds, usd_cents_per_hour=self._usd_cents_per_hour)

        outcome = await post(
            f"{self._url}?language={language.value}&format=detailed",
            headers={
                "Ocp-Apim-Subscription-Key": self._key,
                "Content-Type": content_type,
                "Accept": "application/json",
                "Pronunciation-Assessment": assessment_header(reference_text or "", language),
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
        return parse(body, language=language, cost=cost, scripted=scripted)


def assessment_header(reference_text: str, language: Language) -> str:
    """Base64 JSON, as the endpoint requires. Prosody exists for en-US only.

    An empty reference asks for an unscripted assessment; miscue detection
    (omissions, insertions) only means something against a reference.
    """
    params: dict[str, Any] = {
        "ReferenceText": reference_text,
        "GradingSystem": "HundredMark",
        "Granularity": "Phoneme",
        "Dimension": "Comprehensive",
        "EnableMiscue": bool(reference_text),
    }
    if language is Language.EN_US:
        params["EnableProsodyAssessment"] = True
    return base64.b64encode(json.dumps(params, ensure_ascii=False).encode()).decode("ascii")


def inspect_audio(audio: bytes, audio_format: str) -> tuple[str, float] | PronunciationResult:
    """The Content-Type to send and the length to bill, or why it cannot be sent."""
    if audio_format == "ogg":
        # Telegram voice notes. The length is read from the container (Q8,
        # D-142); billing every one as 60 s throttled learners twelve times early.
        length = opus_seconds(audio)
        if length is None:
            return _failure(
                "scoring.unsupported_audio", f"not a readable Ogg Opus recording; {_FFMPEG_HINT}"
            )
        return _too_long(length) or ("audio/ogg; codecs=opus", length)
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
    return _too_long(seconds) or (
        f"audio/wav; codecs=audio/pcm; samplerate={_SAMPLE_RATE}",
        seconds,
    )


def _too_long(seconds: float) -> PronunciationResult | None:
    """The refusal for a recording longer than the endpoint takes, else None.

    Used as ``_too_long(s) or accepted``: a PronunciationResult is a plain
    dataclass and always truthy.
    """
    if seconds > _MAX_SECONDS:
        return _failure(
            "scoring.audio_too_long", f"{seconds:.1f}s; the endpoint takes {_MAX_SECONDS:.0f}s"
        )
    return None


def estimate_cost(seconds: float, *, usd_cents_per_hour: int) -> int:
    """Thousandths of a US cent, rounded up (D-136), for a recording of known length.

    The rate is SCORING_PRICE_USD_CENTS_PER_HOUR: the owner sets cost caps from
    pricing, so the estimate they are compared against is configuration too.
    """
    return math.ceil(seconds * usd_cents_per_hour * _MILLICENTS_PER_CENT / 3600)


def parse(
    body: Mapping[str, Any], *, language: Language, cost: int, scripted: bool = True
) -> PronunciationResult:
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
            # Azure reports completeness unscripted too, against what it heard;
            # base.py reserves it for scripted, where it means something.
            completeness_score=_score(scores, "CompletenessScore") if scripted else None,
            prosody_score=_score(scores, "ProsodyScore") if language is Language.EN_US else None,
            tone_score=tone_score(words) if language is Language.ZH_CN else None,
            words=words,
            raw=raw,
            cost_usd_millicents=cost,
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
        ok=False, error_code=code, error_message=message, cost_usd_millicents=cost, raw=raw or {}
    )


__all__ = ["AzureScorer", "assessment_header", "estimate_cost", "inspect_audio", "parse"]
