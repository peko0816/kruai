"""FakeTTS has to produce a real MPEG stream, not a plausible-looking blob.

build_pack.py copies duration_ms and len(audio) straight into media_assets, and
the client plays whatever lands in object storage. If the stated duration and
the actual stream disagree, every pack built before M0-2 carries the error and
nothing complains until a person listens.

So the frame parser below decodes bitrate and sample rate out of each header and
derives the frame length from those bits, rather than trusting the constants in
fake.py. If those constants were wrong, this walk would desynchronise and fail.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.services.tts.base import Voice
from app.services.tts.fake import FakeTTS, estimate_duration_ms, silent_mp3


@dataclass(frozen=True)
class MediaAssetRow:
    """The columns build_pack.py fills in from a synthesis result.

    ``size_bytes`` maps to the media_assets column spelled ``bytes``; naming the
    field that here would shadow the builtin for the annotation below it.
    """

    provider: str
    duration_ms: int
    size_bytes: int
    audio: bytes


# MPEG-1 Layer III tables, from the spec rather than from fake.py.
_BITRATES_KBPS = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0)
_SAMPLE_RATES = (44100, 48000, 32000, 0)
_SAMPLES_PER_FRAME = 1152

ZH = "我要一杯水"
KM = "ខ្ញុំចង់បានទឹកមួយកែវ"


def parse_frames(data: bytes) -> list[dict[str, int]]:
    """Walk an MPEG stream, validating every header. Raises on a malformed one."""
    frames: list[dict[str, int]] = []
    offset = 0
    while offset < len(data):
        assert offset + 4 <= len(data), f"truncated header at byte {offset}"
        header = data[offset : offset + 4]

        assert header[0] == 0xFF and header[1] & 0xE0 == 0xE0, (
            f"lost frame sync at byte {offset}: {header.hex()}"
        )
        version = (header[1] >> 3) & 0b11
        layer = (header[1] >> 1) & 0b11
        assert version == 0b11, f"expected MPEG-1, got version bits {version:02b}"
        assert layer == 0b01, f"expected Layer III, got layer bits {layer:02b}"

        bitrate = _BITRATES_KBPS[(header[2] >> 4) & 0b1111]
        sample_rate = _SAMPLE_RATES[(header[2] >> 2) & 0b11]
        padding = (header[2] >> 1) & 0b1
        assert bitrate and sample_rate, f"reserved bitrate/sample-rate at byte {offset}"

        size = (144 * bitrate * 1000) // sample_rate + padding
        frames.append({"offset": offset, "size": size, "sample_rate": sample_rate})
        offset += size

    assert offset == len(data), f"stream ends mid-frame: {offset} != {len(data)}"
    return frames


def measured_duration_ms(data: bytes) -> int:
    """Duration derived from the stream itself, not from what the caller claimed."""
    frames = parse_frames(data)
    return round(sum(_SAMPLES_PER_FRAME * 1000 / f["sample_rate"] for f in frames))


# ------------------------------------------------------- the stream is genuine


async def test_output_is_a_well_formed_mpeg_stream() -> None:
    result = await FakeTTS().synthesize(ZH, voice=Voice.ZH_MODEL)
    frames = parse_frames(result.audio)

    assert frames, "no frames produced"
    assert all(f["sample_rate"] == 48000 for f in frames)
    assert all(f["size"] == 192 for f in frames)


async def test_stated_duration_equals_measured_duration() -> None:
    """The bug this whole design exists to prevent."""
    result = await FakeTTS().synthesize(ZH, voice=Voice.ZH_MODEL)
    assert measured_duration_ms(result.audio) == result.duration_ms


@pytest.mark.parametrize("text", [ZH, KM, "I want a glass of water", "水", "a"])
async def test_duration_matches_for_every_script(text: str) -> None:
    result = await FakeTTS().synthesize(text, voice=Voice.KM_NARRATOR)
    assert measured_duration_ms(result.audio) == result.duration_ms


async def test_byte_length_is_consistent_with_duration() -> None:
    """media_assets stores both; they must not be able to disagree."""
    result = await FakeTTS().synthesize(ZH, voice=Voice.ZH_MODEL)
    assert len(result.audio) == (result.duration_ms // 24) * 192


def test_silent_mp3_rejects_a_duration_off_the_frame_grid() -> None:
    with pytest.raises(ValueError, match="multiple of 24"):
        silent_mp3(100)


# ----------------------------------------------------------------- determinism


async def test_same_text_and_voice_give_identical_bytes() -> None:
    first = await FakeTTS().synthesize(ZH, voice=Voice.ZH_MODEL)
    second = await FakeTTS().synthesize(ZH, voice=Voice.ZH_MODEL)
    assert first.audio == second.audio
    assert first.duration_ms == second.duration_ms


async def test_longer_text_gives_longer_audio() -> None:
    short = await FakeTTS().synthesize("水", voice=Voice.ZH_MODEL)
    long = await FakeTTS().synthesize("我要一杯水谢谢你", voice=Voice.ZH_MODEL)
    assert long.duration_ms > short.duration_ms


async def test_chinese_characters_take_longer_than_latin_ones() -> None:
    """A character of Chinese carries far more than a letter; equal timing would
    make every zh duration implausible."""
    chinese = await FakeTTS().synthesize("水水水", voice=Voice.ZH_MODEL)
    latin = await FakeTTS().synthesize("abc", voice=Voice.EN_MODEL)
    assert chinese.duration_ms > latin.duration_ms


async def test_whitespace_does_not_add_duration() -> None:
    packed = await FakeTTS().synthesize("abc", voice=Voice.EN_MODEL)
    spaced = await FakeTTS().synthesize("a b c", voice=Voice.EN_MODEL)
    assert packed.duration_ms == spaced.duration_ms


# --------------------------------------------------------------- speaking rate


async def test_slower_rate_makes_longer_audio() -> None:
    """TTS_SPEAKING_RATE_ZH defaults to 0.9 so learners can copy the model."""
    normal = await FakeTTS().synthesize(ZH, voice=Voice.ZH_MODEL, speaking_rate=1.0)
    slow = await FakeTTS().synthesize(ZH, voice=Voice.ZH_MODEL, speaking_rate=0.9)
    assert slow.duration_ms > normal.duration_ms


async def test_faster_rate_makes_shorter_audio() -> None:
    normal = await FakeTTS().synthesize(ZH, voice=Voice.ZH_MODEL, speaking_rate=1.0)
    fast = await FakeTTS().synthesize(ZH, voice=Voice.ZH_MODEL, speaking_rate=2.0)
    assert fast.duration_ms < normal.duration_ms


async def test_rate_change_keeps_the_stream_honest() -> None:
    result = await FakeTTS().synthesize(ZH, voice=Voice.ZH_MODEL, speaking_rate=0.9)
    assert measured_duration_ms(result.audio) == result.duration_ms


def test_estimated_duration_always_lands_on_the_frame_grid() -> None:
    for rate in (0.5, 0.9, 1.0, 1.3, 2.0):
        for text in (ZH, KM, "a", "hello world"):
            assert estimate_duration_ms(text, rate) % 24 == 0


# ------------------------------------------------------- failures, not raises


@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
async def test_empty_text_is_reported_not_raised(text: str) -> None:
    result = await FakeTTS().synthesize(text, voice=Voice.ZH_MODEL)
    assert result.ok is False
    assert result.error_code == "tts.empty_text"
    assert result.audio == b""


@pytest.mark.parametrize("rate", [0.0, -1.0])
async def test_non_positive_speaking_rate_is_rejected(rate: float) -> None:
    result = await FakeTTS().synthesize(ZH, voice=Voice.ZH_MODEL, speaking_rate=rate)
    assert result.ok is False
    assert result.error_code == "tts.invalid_speaking_rate"


async def test_non_mp3_format_is_refused_rather_than_mislabelled() -> None:
    """Returning mp3 bytes as "wav" would corrupt the pack silently."""
    result = await FakeTTS().synthesize(ZH, voice=Voice.ZH_MODEL, audio_format="wav")
    assert result.ok is False
    assert result.error_code == "tts.unsupported_format"


async def test_failures_carry_no_cost_or_audio() -> None:
    result = await FakeTTS().synthesize("", voice=Voice.ZH_MODEL)
    assert result.cost_usd_cents == 0
    assert result.duration_ms == 0


# -------------------------------------------------------------------- contract


def test_every_logical_voice_is_available() -> None:
    """A missing voice would stall a pack build on the item that needs it."""
    voices = FakeTTS().available_voices()
    assert set(voices) == set(Voice)


async def test_resolved_voice_says_the_audio_is_synthetic() -> None:
    """This lands in media_assets.provider; a fabricated vendor name there would
    survive into a pack that looks real."""
    result = await FakeTTS().synthesize(ZH, voice=Voice.KM_NARRATOR)
    assert result.resolved_voice == "fake"


async def test_output_format_is_declared() -> None:
    result = await FakeTTS().synthesize(ZH, voice=Voice.ZH_MODEL)
    assert result.audio_format == "mp3"


async def test_short_text_costs_nothing_measurable() -> None:
    """PRD 11.3 puts pack audio at negligible; rounding every call up to a cent
    would make a 10k-item build look like $100."""
    result = await FakeTTS().synthesize(ZH, voice=Voice.ZH_MODEL)
    assert result.cost_usd_cents == 0


async def test_large_text_does_accrue_cost() -> None:
    result = await FakeTTS().synthesize("字" * 2000, voice=Voice.ZH_MODEL)
    assert result.cost_usd_cents == 3


# ------------------------------------------------- what build_pack.py will do


async def test_a_pack_build_produces_consistent_media_metadata() -> None:
    """BACKLOG B2 acceptance: usable from the pipeline.

    Mirrors build_pack.py's loop — synthesise each fixed string, then record the
    duration and byte count the way import_pack.py will read them back.
    """
    items = [
        (Voice.KM_NARRATOR, "ការពន្យល់អំពីទម្រង់ប្រយោគ"),
        (Voice.ZH_MODEL, "我要一杯水"),
        (Voice.KM_FEEDBACK, "ល្អណាស់"),
    ]
    provider = FakeTTS()

    assets: list[MediaAssetRow] = []
    for voice, text in items:
        result = await provider.synthesize(text, voice=voice, speaking_rate=0.9)
        assert result.ok, result.error_message
        assets.append(
            MediaAssetRow(
                provider=result.resolved_voice,
                duration_ms=result.duration_ms,
                size_bytes=len(result.audio),
                audio=result.audio,
            )
        )

    assert len(assets) == len(items)
    for asset in assets:
        assert asset.provider == "fake"
        assert asset.duration_ms > 0
        assert asset.size_bytes > 0
        assert measured_duration_ms(asset.audio) == asset.duration_ms
