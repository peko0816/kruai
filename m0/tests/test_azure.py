"""The Azure verification adapters (m0/azure/), tested without Azure.

Nothing here touches the network: every request goes to an httpx
MockTransport. That matters more than usual because the first real request
will be made by the M0 scripts against recordings that took a week to collect,
and an adapter that mistakes a 429 for a bad recording, or a 44.1 kHz file for
a silent one, would cost the run rather than a test.

The response fixtures follow Microsoft's published REST examples. They have
not been checked against a live response yet, which is why the parser is
tested against both shapes the documentation shows and against garbage.
"""

from __future__ import annotations

import base64
import io
import json
import wave
from typing import Any

import httpx
import pytest

from app.services.provider_errors import ProviderConfigurationError
from app.services.scoring.base import AssessMode, Language
from app.services.tts.base import Voice
from m0.azure.scoring import AzureScorer, assessment_header, estimate_cost, parse
from m0.azure.transport import MAX_ATTEMPTS, MAX_WAIT_SECONDS, post
from m0.azure.tts import AzureTTS, ssml

KEY = "test-key-not-a-real-one"


class Recorder:
    """A transport that answers from a script and remembers what it was sent."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self._responses.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


async def no_sleep(_: float) -> None:
    return None


def wav(*, seconds: float = 1.0, rate: int = 16_000, channels: int = 1, width: int = 2) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.setframerate(rate)
        writer.writeframes(bytes(int(seconds * rate) * channels * width))
    return buffer.getvalue()


# ------------------------------------------------------------------ transport


async def test_a_429_is_waited_out_and_retried() -> None:
    """The free tier throttles at about twenty a minute; a thirty-clip export
    meets that, and failing there would be a failure about nothing."""
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    recorder = Recorder(httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200))

    outcome = await post(
        "https://x",
        headers={},
        content=b"",
        timeout_seconds=1,
        transport=recorder.transport,
        sleep=sleep,
    )

    assert outcome.ok
    assert waits == [7.0]
    assert len(recorder.requests) == 2


async def test_retrying_gives_up_and_says_rate_limited() -> None:
    recorder = Recorder(*[httpx.Response(429) for _ in range(MAX_ATTEMPTS)])

    outcome = await post(
        "https://x",
        headers={},
        content=b"",
        timeout_seconds=1,
        transport=recorder.transport,
        sleep=no_sleep,
    )

    assert outcome.error == "rate_limited"
    assert len(recorder.requests) == MAX_ATTEMPTS


async def test_an_absurd_retry_after_is_capped() -> None:
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    recorder = Recorder(httpx.Response(429, headers={"Retry-After": "86400"}), httpx.Response(200))
    await post(
        "https://x",
        headers={},
        content=b"",
        timeout_seconds=1,
        transport=recorder.transport,
        sleep=sleep,
    )

    assert waits == [MAX_WAIT_SECONDS]


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (httpx.Response(401), "auth_failed"),
        (httpx.Response(403), "auth_failed"),
        (httpx.Response(400, text="bad ssml"), "http_error"),
        (httpx.ReadTimeout("slow"), "timeout"),
        (httpx.ConnectError("down"), "network"),
    ],
)
async def test_every_failure_is_an_outcome_not_an_exception(
    answer: httpx.Response | Exception, error: str
) -> None:
    """The interfaces forbid raising, and only one retry case is worth it:
    a 401 or a malformed request will say the same thing the second time."""
    recorder = Recorder(answer)

    outcome = await post(
        "https://x",
        headers={},
        content=b"",
        timeout_seconds=1,
        transport=recorder.transport,
        sleep=no_sleep,
    )

    assert outcome.error == error
    assert len(recorder.requests) == 1


# ------------------------------------------------------------------------ TTS


def tts(recorder: Recorder, **kwargs: Any) -> AzureTTS:
    return AzureTTS(
        key=KEY, region="eastasia", transport=recorder.transport, sleep=no_sleep, **kwargs
    )


def test_tts_without_a_key_refuses_to_be_built() -> None:
    """Thirty identical 401s is a worse way to learn the key is missing."""
    with pytest.raises(ProviderConfigurationError, match="AZURE_SPEECH_KEY"):
        AzureTTS(key="", region="eastasia")


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


# -------------------------------------------------------------------- scoring

#: Microsoft's REST example shape: scores sit directly on each node.
FLAT: dict[str, Any] = {
    "RecognitionStatus": "Success",
    "DisplayText": "我要水。",
    "NBest": [
        {
            "Display": "我要水。",
            "PronScore": 84.4,
            "AccuracyScore": 90.0,
            "FluencyScore": 74.0,
            "CompletenessScore": 100.0,
            "Words": [
                {
                    "Word": "我",
                    "AccuracyScore": 95.0,
                    "ErrorType": "None",
                    "Phonemes": [
                        {
                            "Phoneme": "w o 3",
                            "AccuracyScore": 95.0,
                            "Offset": 500000,
                            "Duration": 1200000,
                        },
                    ],
                },
                {"Word": "要", "AccuracyScore": 40.0, "ErrorType": "Mispronunciation"},
                {"Word": "水", "ErrorType": "Omission"},
            ],
        }
    ],
}


def nested(flat: dict[str, Any]) -> dict[str, Any]:
    """The SDK's shape: the same scores, under PronunciationAssessment."""
    best = flat["NBest"][0]
    scores = ("PronScore", "AccuracyScore", "FluencyScore", "CompletenessScore", "ErrorType")

    def move(node: dict[str, Any]) -> dict[str, Any]:
        kept = {k: v for k, v in node.items() if k not in scores}
        kept["PronunciationAssessment"] = {k: v for k, v in node.items() if k in scores}
        return kept

    new_best = move(best)
    new_best["Words"] = [
        {**move(w), "Phonemes": [move(p) for p in w.get("Phonemes", [])]} for w in best["Words"]
    ]
    return {**flat, "NBest": [new_best]}


def scorer(recorder: Recorder) -> AzureScorer:
    return AzureScorer(
        key=KEY,
        region="eastasia",
        timeout_seconds=1,
        transport=recorder.transport,
        sleep=no_sleep,
    )


@pytest.mark.parametrize("shape", [FLAT, nested(FLAT)], ids=["rest", "sdk"])
def test_both_documented_shapes_parse_the_same(shape: dict[str, Any]) -> None:
    result = parse(shape, language=Language.ZH_CN, cost=1)

    assert result.ok
    assert result.pron_score == 84.4
    assert result.completeness_score == 100.0
    assert [w.error_type for w in result.words] == [None, "Mispronunciation", "Omission"]
    assert result.words[0].phonemes[0].phoneme == "w o 3"
    assert result.words[0].phonemes[0].offset_ms == 50
    assert result.words[2].accuracy == 0.0


def test_chinese_has_no_prosody_and_no_tone_score_yet() -> None:
    """tone_score is what ZH_TONE_MODE decides; filling it in here would
    answer M0-1's question before M0-1 asked it."""
    shape = {**FLAT, "NBest": [{**FLAT["NBest"][0], "ProsodyScore": 70.0}]}

    zh = parse(shape, language=Language.ZH_CN, cost=1)
    en = parse(shape, language=Language.EN_US, cost=1)

    assert zh.prosody_score is None
    assert zh.tone_score is None
    assert en.prosody_score == 70.0


def test_no_speech_is_named_as_such() -> None:
    result = parse({"RecognitionStatus": "NoMatch"}, language=Language.ZH_CN, cost=1)

    assert result.error_code == "scoring.no_speech"


def test_recognised_but_unassessed_is_a_failure() -> None:
    """If the header were ignored the service would still transcribe, and a
    result with no score must not count as a scored sample."""
    shape = {"RecognitionStatus": "Success", "NBest": [{"Display": "我要水。"}]}

    result = parse(shape, language=Language.ZH_CN, cost=1)

    assert result.error_code == "scoring.no_assessment"


def test_an_unexpected_shape_keeps_the_body_for_whoever_reads_it() -> None:
    shape = {"RecognitionStatus": "Success", "NBest": []}

    result = parse(shape, language=Language.ZH_CN, cost=1)

    assert result.error_code == "scoring.malformed_response"
    assert result.raw == shape


def test_the_header_carries_the_reference_and_asks_for_phonemes() -> None:
    zh = json.loads(base64.b64decode(assessment_header("我要水。", Language.ZH_CN)))
    en = json.loads(base64.b64decode(assessment_header("water", Language.EN_US)))

    assert zh["ReferenceText"] == "我要水。"
    assert zh["Granularity"] == "Phoneme"
    assert "EnableProsodyAssessment" not in zh
    assert en["EnableProsodyAssessment"] is True


async def test_a_good_recording_is_sent_and_scored() -> None:
    recorder = Recorder(httpx.Response(200, json=FLAT))

    result = await scorer(recorder).assess(
        wav(seconds=3), language=Language.ZH_CN, mode=AssessMode.SCRIPTED, reference_text="我要水。"
    )

    request = recorder.requests[0]
    assert request.url.host == "eastasia.stt.speech.microsoft.com"
    assert request.url.params["language"] == "zh-CN"
    assert request.headers["Content-Type"] == "audio/wav; codecs=audio/pcm; samplerate=16000"
    assert result.ok
    assert result.cost_usd_cents == 1


@pytest.mark.parametrize(
    ("audio", "audio_format", "code"),
    [
        (wav(rate=44_100), "wav", "scoring.unsupported_audio"),
        (wav(channels=2), "wav", "scoring.unsupported_audio"),
        (wav(width=1), "wav", "scoring.unsupported_audio"),
        (b"not a wav", "wav", "scoring.unsupported_audio"),
        (wav(seconds=61), "wav", "scoring.audio_too_long"),
        (b"ID3", "mp3", "scoring.unsupported_format"),
    ],
    ids=["44k", "stereo", "8bit", "garbage", "too-long", "mp3"],
)
async def test_audio_azure_would_refuse_is_refused_here_for_free(
    audio: bytes, audio_format: str, code: str
) -> None:
    """Phone recordings are usually 44.1 kHz. Catching that before the request
    costs nothing and says how to fix it."""
    recorder = Recorder()

    result = await scorer(recorder).assess(
        audio,
        language=Language.ZH_CN,
        mode=AssessMode.SCRIPTED,
        reference_text="我要水。",
        audio_format=audio_format,
    )

    assert result.error_code == code
    assert result.cost_usd_cents == 0
    assert recorder.requests == []


async def test_the_format_refusal_says_how_to_fix_it() -> None:
    result = await scorer(Recorder()).assess(
        wav(rate=48_000), language=Language.ZH_CN, mode=AssessMode.SCRIPTED, reference_text="x"
    )

    assert "ffmpeg" in (result.error_message or "")


async def test_unscripted_and_missing_reference_are_refused() -> None:
    recorder = Recorder()

    unscripted = await scorer(recorder).assess(
        wav(), language=Language.ZH_CN, mode=AssessMode.UNSCRIPTED
    )
    no_reference = await scorer(recorder).assess(
        wav(), language=Language.ZH_CN, mode=AssessMode.SCRIPTED, reference_text=""
    )

    assert unscripted.error_code == "scoring.unsupported_mode"
    assert no_reference.error_code == "scoring.missing_reference"
    assert recorder.requests == []


def test_scoring_cost_rounds_up_and_assumes_the_worst_when_unknown() -> None:
    """base.py: conservatively overestimate when the real figure is unknown."""
    assert estimate_cost(0.5) == 1
    assert estimate_cost(18) == 1
    assert estimate_cost(19) == 2
    assert estimate_cost(None) == estimate_cost(60)


async def test_a_non_json_200_is_malformed_not_a_crash() -> None:
    recorder = Recorder(httpx.Response(200, text="<html>"))

    result = await scorer(recorder).assess(
        wav(), language=Language.ZH_CN, mode=AssessMode.SCRIPTED, reference_text="我要水。"
    )

    assert result.error_code == "scoring.malformed_response"


def test_scoring_without_a_key_refuses_to_be_built() -> None:
    with pytest.raises(ProviderConfigurationError, match="AZURE_SPEECH_KEY"):
        AzureScorer(key=KEY, region="", timeout_seconds=1)
