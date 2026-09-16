"""Pronunciation scoring.

Exports the registry and the base types only — never a concrete implementation
(ARCHITECTURE section 3). The domain layer importing ``FakeScorer`` directly
would be the same mistake as importing an Azure client: it pins the caller to
one vendor and quietly removes the seam M0 depends on. Tests reach into
``app.services.scoring.fake`` explicitly, which makes that dependency visible.
"""

from app.services.scoring.base import (
    AssessMode,
    Language,
    PhonemeScore,
    PronunciationResult,
    PronunciationScorer,
    WordScore,
)
from app.services.scoring.registry import (
    available_providers,
    build_scorer,
    get_scorer,
    provider_chain,
)

__all__ = [
    "AssessMode",
    "Language",
    "PhonemeScore",
    "PronunciationResult",
    "PronunciationScorer",
    "WordScore",
    "available_providers",
    "build_scorer",
    "get_scorer",
    "provider_chain",
]
