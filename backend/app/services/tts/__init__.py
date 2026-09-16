"""Text to speech.

Build-time only. Nothing in the request path may import this — the runtime
serves URLs that the content pipeline generated hours earlier (base.py module
docstring).

Exports the registry and base types only, never a concrete implementation
(ARCHITECTURE section 3).
"""

from app.services.tts.base import SynthesisResult, TTSProvider, Voice
from app.services.tts.registry import (
    available_providers,
    build_tts,
    get_tts,
    speaking_rate_for,
)

__all__ = [
    "SynthesisResult",
    "TTSProvider",
    "Voice",
    "available_providers",
    "build_tts",
    "get_tts",
    "speaking_rate_for",
]
