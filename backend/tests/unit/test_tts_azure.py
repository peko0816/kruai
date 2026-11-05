"""Azure TTS (app/services/tts/azure.py, BACKLOG E8), tested without Azure."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.services.provider_errors import ProviderConfigurationError
from app.services.tts.azure import AzureTTS, ssml
from app.services.tts.base import Voice
from tests.unit.azure_recorder import KEY, Recorder, no_sleep

#: What the tests read with when they do not pin anything.
VOICES = {
    Voice.KM_NARRATOR: "km-KH-SreymomNeural",
    Voice.KM_FEEDBACK: "km-KH-SreymomNeural",
    Voice.ZH_MODEL: "zh-CN-XiaoxiaoNeural",
}


def tts(recorder: Recorder, **kwargs: Any) -> AzureTTS:
    voices = {**VOICES, **kwargs.pop("voices", {})}
    return AzureTTS(
        key=KEY,
        region="eastasia",
        voices=voices,
        transport=recorder.transport,
        sleep=no_sleep,
        **kwargs,
    )


def test_tts_without_a_key_refuses_to_be_built() -> None:
    """Thirty identical 401s is a worse way to learn the key is missing."""
    with pytest.raises(ProviderConfigurationError, match="AZURE_SPEECH_KEY"):
        AzureTTS(key="", region="eastasia", voices=VOICES)


async def test_a_pinned_voice_is_the_one_requested_and_reported() -> None:
    """The whole blind test is two builds of this class with different voices;
    if the pin did not reach the request, both clips would be the same voice."""
    recorder = Recorder(httpx.Response(200, content=b"\xff" * 6000))
    provider = tts(recorder, voices={Voice.KM_NARRATOR: "km-KH-PisethNeural"})

    result = await provider.synthesize("សួស្តី", voice=Voice.KM_NARRATOR)

    body = recorder.requests[0].content.decode()
    assert 'name="km-KH-PisethNeural"' in body or "name='km-KH-PisethNeural'" in body
    assert result.ok
    assert result.resolved_voice == "km-KH-PisethNeural"


async def test_the_request_is_what_azure_expects() -> None:
    recorder = Recorder(httpx.Response(200, content=b"\xff"))
    await tts(recorder).synthesize("សួស្តី", voice=Voice.KM_NARRATOR)

    request = recorder.requests[0]
    assert str(request.url) == "https://eastasia.tts.speech.microsoft.com/cognitiveservices/v1"
    assert request.headers["Ocp-Apim-Subscription-Key"] == KEY
    assert request.headers["Content-Type"] == "application/ssml+xml"
    assert request.headers["X-Microsoft-OutputFormat"].endswith("-mp3")


def test_ssml_escapes_the_text_and_takes_the_language_from_the_voice() -> None:
    """A Khmer sentence with an ampersand or quotation in it must not become
    an SSML parse error that reads as a synthesis failure."""
    body = ssml('ក <b> & "x"', vendor_voice="km-KH-SreymomNeural", speaking_rate=0.9)

    assert "&lt;b&gt; &amp;" in body
    assert "<b>" not in body
    assert 'xml:lang="km-KH"' in body or "xml:lang='km-KH'" in body
    assert "-10%" in body


def test_a_normal_rate_is_plus_zero() -> None:
    assert "+0%" in ssml("ក", vendor_voice="km-KH-SreymomNeural", speaking_rate=1.0)


async def test_the_duration_matches_the_bytes() -> None:
    """48 kbit/s constant: 6000 bytes is 48000 bits is one second."""
    recorder = Recorder(httpx.Response(200, content=b"\xff" * 6000))

    result = await tts(recorder).synthesize("ក", voice=Voice.KM_NARRATOR)

    assert result.duration_ms == 1000


async def test_tts_cost_is_floored_per_call() -> None:
    """$15 per million characters: a sentence is a fraction of a cent, and
    reporting it as a whole one would inflate a pack build a hundredfold."""
    recorder = Recorder(httpx.Response(200, content=b"\xff"), httpx.Response(200, content=b"\xff"))
    provider = tts(recorder)

    short = await provider.synthesize("ក" * 100, voice=Voice.KM_NARRATOR)
    long = await provider.synthesize("ក" * 2000, voice=Voice.KM_NARRATOR)

    assert short.cost_usd_cents == 0
    assert long.cost_usd_cents == 3


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"text": "  "}, "tts.empty_text"),
        ({"speaking_rate": 0.4}, "tts.invalid_speaking_rate"),
        ({"speaking_rate": 2.1}, "tts.invalid_speaking_rate"),
        ({"audio_format": "wav"}, "tts.unsupported_format"),
        ({"voice": Voice.EN_MODEL}, "tts.unsupported_voice"),
    ],
)
async def test_bad_input_is_refused_without_a_request(kwargs: dict[str, Any], code: str) -> None:
    recorder = Recorder()
    arguments: dict[str, Any] = {"text": "ក", "voice": Voice.KM_NARRATOR, **kwargs}

    result = await tts(recorder).synthesize(arguments.pop("text"), **arguments)

    assert result.error_code == code
    assert recorder.requests == []


async def test_a_refused_request_costs_nothing_and_does_not_leak_the_key() -> None:
    recorder = Recorder(httpx.Response(401, text="Access denied due to invalid subscription key."))

    result = await tts(recorder).synthesize("ក" * 2000, voice=Voice.KM_NARRATOR)

    assert result.error_code == "tts.auth_failed"
    assert result.cost_usd_cents == 0
    assert KEY not in (result.error_message or "")


async def test_a_timeout_is_charged_since_it_may_have_run() -> None:
    recorder = Recorder(httpx.ReadTimeout("slow"))

    result = await tts(recorder).synthesize("ក" * 2000, voice=Voice.KM_NARRATOR)

    assert result.error_code == "tts.timeout"
    assert result.cost_usd_cents == 3


async def test_an_empty_200_is_a_failure_not_silence() -> None:
    recorder = Recorder(httpx.Response(200, content=b""))

    result = await tts(recorder).synthesize("ក", voice=Voice.KM_NARRATOR)

    assert result.error_code == "tts.empty_audio"
