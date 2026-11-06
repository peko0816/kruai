"""Azure scoring (app/services/scoring/azure.py, BACKLOG E9), tested without Azure.

Moved from m0/ with E9 (D-141). Nothing here touches the network: every
request goes to an httpx MockTransport.

The response fixtures follow Microsoft's published REST examples; the flat
shape is the one the live endpoint returned on 2026-11-05. The parser is still
tested against the SDK's nested shape and against garbage.
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
from app.services.scoring.azure import AzureScorer, assessment_header, estimate_cost, parse
from app.services.scoring.base import AssessMode, Language
from tests.unit.azure_recorder import KEY, Recorder, no_sleep
from tests.unit.ogg_builder import opus


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


def scorer(recorder: Recorder, **kwargs: Any) -> AzureScorer:
    kwargs.setdefault("usd_cents_per_hour", 200)
    return AzureScorer(
        key=KEY,
        region="eastasia",
        timeout_seconds=1,
        transport=recorder.transport,
        sleep=no_sleep,
        **kwargs,
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


def test_chinese_has_tone_and_no_prosody_english_the_reverse() -> None:
    """PRD section 8: prosody is en-US only; zh-CN carries tone instead, derived
    from the syllables now that M0-1 has said how (D-140)."""
    shape = {**FLAT, "NBest": [{**FLAT["NBest"][0], "ProsodyScore": 70.0}]}

    zh = parse(shape, language=Language.ZH_CN, cost=1)
    en = parse(shape, language=Language.EN_US, cost=1)

    assert zh.prosody_score is None
    assert zh.tone_score == 95.0
    assert en.prosody_score == 70.0
    assert en.tone_score is None


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
        (b"OggS" + bytes(200), "ogg", "scoring.unsupported_audio"),
        (opus(61), "ogg", "scoring.audio_too_long"),
    ],
    ids=["44k", "stereo", "8bit", "garbage", "too-long", "mp3", "bad-ogg", "long-ogg"],
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


async def test_a_telegram_voice_note_is_billed_by_its_length() -> None:
    """Q8 (D-142): 5 s, not the 60 s every Ogg recording used to be billed as."""
    recorder = Recorder(httpx.Response(200, json=FLAT))

    result = await scorer(recorder).assess(
        opus(5.0),
        language=Language.ZH_CN,
        mode=AssessMode.SCRIPTED,
        reference_text="我要水。",
        audio_format="ogg",
    )

    assert recorder.requests[0].headers["Content-Type"] == "audio/ogg; codecs=opus"
    assert result.ok
    assert result.cost_usd_millicents == 278, "5 s at $2 an hour"


async def test_the_format_refusal_says_how_to_fix_it() -> None:
    result = await scorer(Recorder()).assess(
        wav(rate=48_000), language=Language.ZH_CN, mode=AssessMode.SCRIPTED, reference_text="x"
    )

    assert "ffmpeg" in (result.error_message or "")


async def test_scripted_without_a_reference_and_unscripted_with_one_are_refused() -> None:
    """base.py: SCRIPTED needs reference_text; UNSCRIPTED must not have one."""
    recorder = Recorder()

    no_reference = await scorer(recorder).assess(
        wav(), language=Language.ZH_CN, mode=AssessMode.SCRIPTED, reference_text=""
    )
    with_reference = await scorer(recorder).assess(
        wav(), language=Language.ZH_CN, mode=AssessMode.UNSCRIPTED, reference_text="我要水。"
    )

    assert no_reference.error_code == "scoring.missing_reference"
    assert with_reference.error_code == "scoring.reference_not_allowed"
    assert recorder.requests == []


async def test_an_open_answer_is_assessed_against_what_was_heard() -> None:
    """Unscripted: an empty reference, no miscue detection, no completeness —
    tried for real on 2026-11-05 (D-141)."""
    recorder = Recorder(httpx.Response(200, json=FLAT))

    result = await scorer(recorder).assess(
        wav(seconds=3), language=Language.ZH_CN, mode=AssessMode.UNSCRIPTED
    )

    header = json.loads(base64.b64decode(recorder.requests[0].headers["Pronunciation-Assessment"]))
    assert header["ReferenceText"] == ""
    assert header["EnableMiscue"] is False
    assert result.ok
    assert result.completeness_score is None, "scripted-only (scoring/base.py)"
    assert result.pron_score == 84.4


async def test_tone_is_derived_from_the_syllables_for_chinese_only() -> None:
    """D-140: the tone digit is the expected tone, so the score is the syllables'."""
    recorder = Recorder(httpx.Response(200, json=FLAT), httpx.Response(200, json=FLAT))

    zh = await scorer(recorder).assess(
        wav(), language=Language.ZH_CN, mode=AssessMode.SCRIPTED, reference_text="我要水。"
    )
    en = await scorer(recorder).assess(
        wav(), language=Language.EN_US, mode=AssessMode.SCRIPTED, reference_text="water"
    )

    assert zh.tone_score == 95.0, "the one syllable FLAT carries, at 95"
    assert en.tone_score is None


def test_parse_tone_mode_is_refused_at_construction() -> None:
    """So the startup self-check refuses to boot on it (D-140)."""
    with pytest.raises(ProviderConfigurationError, match="expected"):
        scorer(Recorder(), tone_mode="parse")


async def test_the_price_per_hour_is_configuration() -> None:
    recorder = Recorder(httpx.Response(200, json=FLAT))

    result = await scorer(recorder, usd_cents_per_hour=100).assess(
        wav(seconds=36), language=Language.ZH_CN, mode=AssessMode.SCRIPTED, reference_text="x"
    )

    assert result.cost_usd_millicents == 1000, "36 s at $1 an hour is one cent"


def test_scoring_cost_rounds_up() -> None:
    """base.py: conservatively overestimate. Every accepted recording now has a
    known length (Q8), so rounding up is the only overestimate left."""
    assert estimate_cost(0.5, usd_cents_per_hour=200) == 28
    assert estimate_cost(18, usd_cents_per_hour=200) == 1000
    assert estimate_cost(19, usd_cents_per_hour=200) == 1056


async def test_a_non_json_200_is_malformed_not_a_crash() -> None:
    recorder = Recorder(httpx.Response(200, text="<html>"))

    result = await scorer(recorder).assess(
        wav(), language=Language.ZH_CN, mode=AssessMode.SCRIPTED, reference_text="我要水。"
    )

    assert result.error_code == "scoring.malformed_response"


def test_scoring_without_a_key_refuses_to_be_built() -> None:
    with pytest.raises(ProviderConfigurationError, match="AZURE_SPEECH_KEY"):
        AzureScorer(key=KEY, region="", timeout_seconds=1, usd_cents_per_hour=200)
