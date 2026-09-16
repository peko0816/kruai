"""Chooses a scorer by configuration and routes by language.

Callers ask for a language and get something that implements
``PronunciationScorer``. They never learn which vendor answered, which is what
lets M0-1's decision land as one new file instead of a change to every call site
(ARCHITECTURE 2.1).

Misconfiguration raises a native exception rather than an AppError. A provider
name that does not exist, or a language nothing can score, is a broken
deployment, not a business rule someone can recover from — CODING_STANDARDS
section 5.1 says let those crash. BACKLOG B5 turns this into a startup sweep so
the crash happens at boot rather than on the first learner's recording.
"""

from __future__ import annotations

from collections.abc import Callable

from app.core.config import Settings
from app.services.provider_errors import ProviderConfigurationError
from app.services.scoring.base import Language, PronunciationScorer
from app.services.scoring.fake import FakeScorer

#: Every implementation that exists. Adding a vendor means one entry here and
#: one new module — that is the whole change surface M0 is meant to have.
_FACTORIES: dict[str, Callable[[], PronunciationScorer]] = {
    "fake": FakeScorer,
}


def available_providers() -> tuple[str, ...]:
    """Implemented provider names, for startup checks and error messages."""
    return tuple(sorted(_FACTORIES))


def build_scorer(name: str) -> PronunciationScorer:
    """Instantiate one provider by name.

    Raises:
        ValueError: no such implementation. Names come from SCORING_PROVIDER and
            SCORING_FALLBACK_PROVIDERS, so this is a typo in .env or a provider
            that has not landed yet.
    """
    factory = _FACTORIES.get(name)
    if factory is None:
        raise ProviderConfigurationError(
            f"unknown scoring provider {name!r}; implemented: {list(available_providers())}"
        )
    return factory()


def provider_chain(settings: Settings) -> tuple[str, ...]:
    """Primary provider followed by its fallbacks, in the configured order."""
    return (settings.scoring_provider, *settings.scoring_fallback_providers)


def get_scorer(language: Language, *, settings: Settings) -> PronunciationScorer:
    """First configured provider that supports ``language``.

    Settings is passed in rather than read from the module so that a test, or a
    request handler with its own overrides, does not have to mutate global
    state to change providers.

    Raises:
        ValueError: a configured name has no implementation, or none of them
            support this language. Both are deployment errors; the message
            names the language so the fix is obvious.
    """
    chain = provider_chain(settings)
    for name in chain:
        scorer = build_scorer(name)
        if scorer.supports(language):
            return scorer

    raise ProviderConfigurationError(
        f"no configured scoring provider supports {language.value}; "
        f"tried {list(chain)} (SCORING_PROVIDER + SCORING_FALLBACK_PROVIDERS)"
    )
