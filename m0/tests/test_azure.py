"""The Azure scoring verification adapter (m0/azure/), tested without Azure.

The TTS adapter and the HTTP layer moved to app/services/ with E8 (D-137);
their tests moved with them, to backend/tests/unit/.

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
from m0.azure.scoring import AzureScorer, assessment_header, estimate_cost, parse
from tests.unit.azure_recorder import KEY, Recorder, no_sleep


def wav(*, seconds: float = 1.0, rate: int = 16_000, channels: int = 1, width: int = 2) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.setframerate(rate)
        writer.writeframes(bytes(int(seconds * rate) * channels * width))
    return buffer.getvalue()


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
    assert result.cost_usd_millicents == 167, "3 s at $2 an hour, in thousandths of a cent"


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
    assert result.cost_usd_millicents == 0
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
    assert estimate_cost(0.5) == 28
    assert estimate_cost(18) == 1000
    assert estimate_cost(19) == 1056
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
