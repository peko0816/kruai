"""Well-formed Ogg Opus files for tests, built page by page (RFC 3533, RFC 7845).

No Opus encoder is available where these tests run, and none is needed: the
length lives in the container, so the packets inside can be any bytes. Pages
carry correct checksums, so a file built here is one a strict reader accepts.
"""

from __future__ import annotations

import struct

OPUS_RATE = 48_000
#: What libopus writes; Telegram's voice notes carry the same.
DEFAULT_PRE_SKIP = 312

_BOS, _EOS = 0x02, 0x04


def _crc_table() -> list[int]:
    table = []
    for byte in range(256):
        crc = byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) if crc & 0x80000000 else crc << 1
        table.append(crc & 0xFFFFFFFF)
    return table


_TABLE = _crc_table()


def _crc(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc = ((crc << 8) ^ _TABLE[((crc >> 24) ^ byte) & 0xFF]) & 0xFFFFFFFF
    return crc


def page(body: bytes, *, granule: int, serial: int = 1, sequence: int = 0, flags: int = 0) -> bytes:
    """One page holding one packet."""
    lacing = [255] * (len(body) // 255) + [len(body) % 255]
    header = struct.pack("<4sBBqIIIB", b"OggS", 0, flags, granule, serial, sequence, 0, len(lacing))
    unsigned = header + bytes(lacing) + body
    checksum = _crc(unsigned)
    return unsigned[:22] + struct.pack("<I", checksum) + unsigned[26:]


def opus_head(pre_skip: int = DEFAULT_PRE_SKIP) -> bytes:
    return b"OpusHead" + struct.pack("<BBHIhB", 1, 1, pre_skip, 16_000, 0, 0)


def opus(
    seconds: float, *, pages: int = 3, pre_skip: int = DEFAULT_PRE_SKIP, serial: int = 1
) -> bytes:
    """An Ogg Opus file whose speech lasts ``seconds``, over ``pages`` audio pages."""
    total = pre_skip + round(seconds * OPUS_RATE)
    out = [
        page(opus_head(pre_skip), granule=0, serial=serial, sequence=0, flags=_BOS),
        page(b"OpusTags" + bytes(16), granule=0, serial=serial, sequence=1),
    ]
    for index in range(1, pages + 1):
        out.append(
            page(
                bytes(300),  # longer than one lacing value, so the table has two
                granule=total * index // pages,
                serial=serial,
                sequence=1 + index,
                flags=_EOS if index == pages else 0,
            )
        )
    return b"".join(out)
