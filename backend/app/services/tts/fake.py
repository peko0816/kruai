"""Deterministic stand-in for a real TTS vendor.

M0-2 has not picked a Khmer voice yet, so every pack built before then is built
against this. That means it cannot return a token blob: ``build_pack.py`` writes
``media_assets.duration_ms`` and ``bytes`` straight from the result, and the
client eventually plays the file. A placeholder whose stated duration does not
match its actual length would let a whole class of bug through — the pack looks
right, the audio does not line up, and nothing fails until someone listens.

So this emits a real MPEG audio stream: a run of MPEG-1 Layer III frames with a
valid header and a zeroed body, which decoders render as silence. Frames are
built by hand rather than through an encoder because a fake that needs a native
codec to produce a placeholder has the dependency backwards.

Note this is a build-time provider. Anything that calls it inside a request is
wrong (see the module docstring on base.py).
"""

from __future__ import annotations

import math
from typing import Final

from app.services.tts.base import SynthesisResult, TTSProvider, Voice

# --------------------------------------------------------------------- MPEG-1
# MPEG-1 Layer III, 64 kbps, 48 kHz, mono, no CRC, no padding:
#
#   FF        11111111  sync
#   FB        111 11 01 1   sync / version 1 / layer III / no CRC
#   54        0101 01 0 0   64 kbps / 48 kHz / no padding / not private
#   C4        11 00 0 1 00  mono / no extension / not copyright / original
#
# Chosen because 48 kHz makes the arithmetic exact: a Layer III frame is always
# 1152 samples, so 1152/48000 is 24 ms on the nose, and 144*64000/48000 is 192
# bytes with no padding frames. At 44.1 kHz both come out fractional and the
# stated duration could only ever approximate the real one.
_FRAME_HEADER: Final = bytes((0xFF, 0xFB, 0x54, 0xC4))
_FRAME_BYTES: Final = 192
_FRAME_MS: Final = 24

# ------------------------------------------------------------------- duration
# A silent file still has to be plausibly long, or every duration-handling bug
# downstream hides behind an implausible number. These are rough measured
# speaking rates, not configuration: nothing about real usage data would change
# them, which is the test CODING_STANDARDS section 4 sets for what belongs in
# CONFIG_REFERENCE.
_MS_PER_CJK_CHAR: Final = 260  # ~4 characters a second
_MS_PER_OTHER_CHAR: Final = 65  # Khmer, Latin: ~15 characters a second

#: Azure bills TTS around $16 per million characters, so a sentence costs a
#: small fraction of a cent. Integer division reports that honestly as 0 rather
#: than rounding every call up to 1 — PRD 11.3 puts pack audio at "negligible",
#: and a cent per item would make a 10k-item build look like $100.
_CHARS_PER_USD_CENT: Final = 625


def _is_cjk(char: str) -> bool:
    return "一" <= char <= "鿿"


def estimate_duration_ms(text: str, speaking_rate: float) -> int:
    """Plausible spoken length, rounded up to a whole number of frames.

    Rounding up to the frame grid is what keeps ``duration_ms`` exactly equal to
    the length of the bytes returned beside it.
    """
    spoken = sum(
        _MS_PER_CJK_CHAR if _is_cjk(char) else _MS_PER_OTHER_CHAR
        for char in text
        if not char.isspace()
    )
    frames = math.ceil(spoken / speaking_rate / _FRAME_MS)
    return frames * _FRAME_MS


def silent_mp3(duration_ms: int) -> bytes:
    """A decodable MPEG-1 Layer III stream of silence.

    Args:
        duration_ms: must be a multiple of the 24 ms frame length; callers get
            that from estimate_duration_ms.
    """
    if duration_ms % _FRAME_MS:
        raise ValueError(f"duration_ms must be a multiple of {_FRAME_MS}, got {duration_ms}")
    frame = _FRAME_HEADER + bytes(_FRAME_BYTES - len(_FRAME_HEADER))
    return frame * (duration_ms // _FRAME_MS)


class FakeTTS(TTSProvider):
    """Synthesises silence whose length tracks the text. No vendor, no codec."""

    name = "fake"

    def available_voices(self) -> dict[Voice, str]:
        """Every logical voice, so pack builds never stall on a missing one.

        All of them resolve to "fake" (ARCHITECTURE 3.2). This string lands in
        ``media_assets.provider``, where the useful fact is that the audio is
        synthetic — a fabricated vendor voice name there would be worse than
        useless, since it would survive into a pack that looks real.
        """
        return dict.fromkeys(Voice, self.name)

    async def synthesize(
        self,
        text: str,
        *,
        voice: Voice,
        speaking_rate: float = 1.0,
        audio_format: str = "mp3",
    ) -> SynthesisResult:
        invalid = self._reject(text, speaking_rate=speaking_rate, audio_format=audio_format)
        if invalid is not None:
            return invalid

        duration_ms = estimate_duration_ms(text, speaking_rate)
        audio = silent_mp3(duration_ms)
        billable = len([char for char in text if not char.isspace()])

        return SynthesisResult(
            ok=True,
            audio=audio,
            audio_format="mp3",
            duration_ms=duration_ms,
            resolved_voice=self.name,
            cost_usd_cents=billable // _CHARS_PER_USD_CENT,
        )

    def _reject(
        self, text: str, *, speaking_rate: float, audio_format: str
    ) -> SynthesisResult | None:
        """Bad input comes back as ok=False; the contract forbids raising.

        build_pack.py marks the item media_pending and carries on, so a single
        unspeakable string cannot abort a whole pack (ARCHITECTURE section 5).
        """
        if not text.strip():
            return _failure("tts.empty_text", "nothing to synthesise")
        if speaking_rate <= 0:
            return _failure(
                "tts.invalid_speaking_rate", f"speaking_rate must be > 0, got {speaking_rate}"
            )
        if audio_format != "mp3":
            # Better to say so than to hand back mp3 bytes under a wav filename.
            return _failure(
                "tts.unsupported_format", f"{self.name} emits mp3 only, asked for {audio_format!r}"
            )
        return None


def _failure(code: str, message: str) -> SynthesisResult:
    return SynthesisResult(ok=False, error_code=code, error_message=message, cost_usd_cents=0)


__all__ = ["FakeTTS", "estimate_duration_ms", "silent_mp3"]
