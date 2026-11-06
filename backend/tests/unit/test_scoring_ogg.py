"""The length of a Telegram voice note, read from its Ogg container (Q8, D-142)."""

from __future__ import annotations

import pytest

from app.services.scoring.ogg import opus_seconds
from tests.unit.ogg_builder import _crc, opus, opus_head, page


def test_the_builder_checksums_like_ogg() -> None:
    """CRC-32, polynomial 0x04C11DB7, no reflection, zero init and xor-out:
    the published check value for "123456789"."""
    assert _crc(b"123456789") == 0x89A1897F


@pytest.mark.parametrize("seconds", [0.5, 5.0, 59.9])
def test_the_length_is_the_last_granule_less_the_pre_skip(seconds: float) -> None:
    assert opus_seconds(opus(seconds)) == pytest.approx(seconds, abs=1e-4)


def test_pre_skip_is_priming_not_speech() -> None:
    """The same speech with a longer encoder delay is the same length."""
    assert opus_seconds(opus(5.0, pre_skip=3840)) == pytest.approx(5.0, abs=1e-4)


def test_a_page_on_which_no_packet_ends_says_nothing() -> None:
    """Granule -1: the packet continues on the next page. It must not count as
    a length, nor reset one already read."""
    recording = opus(5.0, pages=1) + page(bytes(10), granule=-1, sequence=9)

    assert opus_seconds(recording) == pytest.approx(5.0, abs=1e-4)


def test_another_stream_in_the_file_is_not_this_ones_length() -> None:
    recording = opus(5.0) + page(bytes(10), granule=48_000 * 50, serial=2, sequence=0)

    assert opus_seconds(recording) == pytest.approx(5.0, abs=1e-4)


def test_headers_with_no_audio_are_zero_seconds() -> None:
    recording = page(opus_head(), granule=0, flags=0x02) + page(b"OpusTags", granule=0, sequence=1)

    assert opus_seconds(recording) == 0.0


@pytest.mark.parametrize(
    "recording",
    [
        b"",
        b"RIFF" + bytes(40),
        opus(5.0)[:-10],  # cut off mid-page
        opus(5.0) + b"OggS",  # a partial header after the last page
        opus(5.0).replace(b"OpusHead", b"OpusHEAD"),  # not Opus (Vorbis, Speex...)
        page(opus_head(), granule=0, flags=0x02),  # identification only, no pages after
        opus(5.0).replace(b"OggS\x00", b"OggS\x01", 1),  # unknown Ogg version
    ],
    ids=["empty", "wav", "truncated", "trailing", "not-opus", "head-only", "version"],
)
def test_anything_else_is_not_read(recording: bytes) -> None:
    assert opus_seconds(recording) is None
