"""How long an Ogg Opus recording is, read from its container (BACKLOG Q8, D-142).

Telegram voice notes are Ogg Opus. Without their length the scorer billed each
one as the longest the endpoint takes, 60 seconds — twelve times a typical
five-second sentence — and the cost guard throttled learners on that estimate.

The length is in the container, no decoding needed (RFC 7845): every Ogg page
carries a *granule position*, which for Opus is the count of 48 kHz samples
decoded by the end of that page, and the identification header (``OpusHead``)
says how many of the first samples are encoder priming (*pre-skip*) rather
than speech. Length = (last granule - pre-skip) / 48000.

Anything that is not a well-formed single Opus stream reads as ``None``: if
this cannot walk the pages, the provider will not decode them either.
"""

from __future__ import annotations

import struct
from typing import Final

#: Opus granule positions always count 48 kHz samples, whatever the input rate.
_OPUS_RATE: Final = 48_000

#: capture pattern, version, header type, granule position, serial number,
#: page sequence, checksum, segment count — 27 bytes, little-endian.
_PAGE_HEADER: Final = struct.Struct("<4sBBqIIIB")

#: "OpusHead", version, channel count, then pre-skip as an unsigned 16-bit int.
_OPUS_HEAD: Final = b"OpusHead"
_PRE_SKIP_OFFSET: Final = 10
_OPUS_HEAD_LENGTH: Final = 19


def opus_seconds(audio: bytes) -> float | None:
    """Seconds of speech in an Ogg Opus file; None if it is not one."""
    position = 0
    serial: int | None = None
    pre_skip = 0
    last_granule: int | None = None
    while position < len(audio):
        if position + _PAGE_HEADER.size > len(audio):
            return None
        capture, version, _type, granule, page_serial, _sequence, _crc, segments = (
            _PAGE_HEADER.unpack_from(audio, position)
        )
        if capture != b"OggS" or version != 0:
            return None
        table_end = position + _PAGE_HEADER.size + segments
        body_end = table_end + sum(audio[position + _PAGE_HEADER.size : table_end])
        if table_end > len(audio) or body_end > len(audio):
            return None
        body = audio[table_end:body_end]
        if serial is None:
            # The first page of an Opus stream holds the identification header alone.
            if not body.startswith(_OPUS_HEAD) or len(body) < _OPUS_HEAD_LENGTH:
                return None
            serial = page_serial
            (pre_skip,) = struct.unpack_from("<H", body, _PRE_SKIP_OFFSET)
        elif page_serial == serial and granule >= 0:
            # -1 marks a page on which no packet ends; it says nothing about length.
            last_granule = granule
        position = body_end
    if last_granule is None:
        return None
    return max(last_granule - pre_skip, 0) / _OPUS_RATE


__all__ = ["opus_seconds"]
