"""Chooses a TTS provider by configuration and routes by logical voice.

Routing differs from scoring's: a scorer is picked by language, a synthesiser by
the voice the caller asked for, because that is the axis vendors actually differ
on. M0-2 exists precisely because Azure may have no km-KH voice at all, in which
case Khmer narration comes from one vendor and Chinese demonstration from
another.

There is no TTS_FALLBACK_PROVIDERS in CONFIG_REFERENCE, so this resolves a
single configured provider rather than walking a chain. Inventing the key here
would be a silent config addition, which CLAUDE.md R3 rules out.
"""

from __future__ import annotations

from collections.abc import Callable

from app.core.config import Settings
from app.services.provider_errors import ProviderConfigurationError
from app.services.tts.base import TTSProvider, Voice
from app.services.tts.fake import FakeTTS

_FACTORIES: dict[str, Callable[[], TTSProvider]] = {
    "fake": FakeTTS,
}


def available_providers() -> tuple[str, ...]:
    return tuple(sorted(_FACTORIES))


def build_tts(name: str) -> TTSProvider:
    """Instantiate one provider by name.

    Raises:
        ValueError: no such implementation — a typo in TTS_PROVIDER, or a vendor
            that has not landed yet.
    """
    factory = _FACTORIES.get(name)
    if factory is None:
        raise ProviderConfigurationError(
            f"unknown tts provider {name!r}; implemented: {list(available_providers())}"
        )
    return factory()


def get_tts(voice: Voice, *, settings: Settings) -> TTSProvider:
    """The configured provider, if it can produce ``voice``.

    Raises:
        ValueError: the provider does not exist, or offers no such voice. Both
            are deployment errors and should stop a pack build immediately —
            silently substituting a different voice would ship a pack where the
            Khmer explanation is read in Chinese.
    """
    provider = build_tts(settings.tts_provider)
    if voice not in provider.available_voices():
        raise ProviderConfigurationError(
            f"tts provider {provider.name!r} has no voice for {voice.value}; "
            f"it offers {sorted(v.value for v in provider.available_voices())}"
        )
    return provider


def speaking_rate_for(voice: Voice, *, settings: Settings) -> float:
    """Configured rate for a voice's language.

    Chinese demonstration is slowed to TTS_SPEAKING_RATE_ZH because learners are
    copying it; Khmer narration runs at TTS_SPEAKING_RATE_KM since it is
    explanation, not a model to imitate.
    """
    if voice is Voice.ZH_MODEL:
        return settings.tts_speaking_rate_zh
    if voice in (Voice.KM_NARRATOR, Voice.KM_FEEDBACK):
        return settings.tts_speaking_rate_km
    return 1.0
