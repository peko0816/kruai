"""Startup capability self-check.

ARCHITECTURE 3.1: the registries verify at boot that the configured providers
cover what the deployment needs, and refuse to start otherwise. The alternative
is discovering it when a learner presses record.

Every problem is collected before anything is raised. A deployment with three
misconfigurations should see three lines, not fix one and rerun to find the
next — the cost of a bad deploy is measured in rounds, not in errors.

Only ProviderConfigurationError counts as a misconfiguration. A different
exception out of a factory is a bug in that factory, and swallowing it here
would report the wrong thing to whoever is on call.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Final

from app.core.config import Settings
from app.core.logging import configure_logging, get_logger
from app.services.entitlements.pricing import (
    BILLING_PERIODS,
    PURCHASABLE_PLANS,
    UnpricedError,
    price_for,
)
from app.services.llm.registry import get_llm
from app.services.payments.registry import build_payment_provider, provider_for_currency
from app.services.provider_errors import ProviderConfigurationError
from app.services.scoring.base import Language
from app.services.scoring.registry import get_scorer
from app.services.tts.base import Voice
from app.services.tts.registry import get_tts

log = get_logger(__name__)

#: PRD 1.3 — V1 ships Chinese only. The schema is language-agnostic and English
#: arrives in v2 with its own content packs; this list changes then, not before.
#: Not configuration: no deployment can serve a language whose packs do not
#: exist, so there is nothing here for an operator to tune.
REQUIRED_LANGUAGES: Final[tuple[Language, ...]] = (Language.ZH_CN,)

#: Khmer is the teaching language and Chinese is what gets modelled (PRD 1.1),
#: so a pack needs all three. EN_MODEL is v2, like the language above.
REQUIRED_VOICES: Final[tuple[Voice, ...]] = (
    Voice.KM_NARRATOR,
    Voice.KM_FEEDBACK,
    Voice.ZH_MODEL,
)


@dataclass(frozen=True)
class CapabilityProblem:
    """One thing the configured providers cannot do."""

    adapter: str
    #: The capability that is missing, named the way an operator would search
    #: for it: a language tag, a voice key, a currency code.
    requirement: str
    detail: str

    def __str__(self) -> str:
        return f"{self.adapter}: cannot serve {self.requirement} — {self.detail}"


def collect_problems(settings: Settings) -> list[CapabilityProblem]:
    """Every capability gap in this configuration. Empty means it is coherent."""
    return [
        *_scoring_problems(settings),
        *_tts_problems(settings),
        *_llm_problems(settings),
        *_payment_problems(settings),
        *_pricing_problems(settings),
    ]


def check_provider_configuration(settings: Settings) -> None:
    """Fail the boot if any configured provider cannot do its job.

    Raises:
        ProviderConfigurationError: listing every problem found, so one restart
            surfaces all of them.
    """
    problems = collect_problems(settings)
    if not problems:
        log.info(
            "selfcheck.passed",
            scoring=settings.scoring_provider,
            tts=settings.tts_provider,
            llm=settings.llm_provider,
            payments=list(settings.payment_providers),
            languages=[language.value for language in REQUIRED_LANGUAGES],
        )
        return

    listing = "\n".join(f"  - {problem}" for problem in problems)
    log.error("selfcheck.failed", problem_count=len(problems))
    raise ProviderConfigurationError(
        f"{len(problems)} provider capability problem(s); the application cannot start:\n{listing}"
    )


def _scoring_problems(settings: Settings) -> list[CapabilityProblem]:
    problems = []
    for language in REQUIRED_LANGUAGES:
        try:
            get_scorer(language, settings=settings)
        except ProviderConfigurationError as exc:
            problems.append(CapabilityProblem("scoring", language.value, str(exc)))
    return problems


def _tts_problems(settings: Settings) -> list[CapabilityProblem]:
    """Checked even though TTS only runs at build time.

    A deployment whose TTS is misconfigured cannot build a pack, and learning
    that at boot beats learning it an hour into a build.
    """
    problems = []
    for voice in REQUIRED_VOICES:
        try:
            get_tts(voice, settings=settings)
        except ProviderConfigurationError as exc:
            problems.append(CapabilityProblem("tts", voice.value, str(exc)))
    return problems


#: REALTIME_PROVIDER is deliberately unchecked. No realtime registry exists yet
#: (BACKLOG F2), so there is nothing to resolve a name against — misconfiguring
#: it today has no symptom and no consequence. Add a _realtime_problems() here
#: when that adapter lands, or a bad value will reach the first Pro session.


def _llm_problems(settings: Settings) -> list[CapabilityProblem]:
    """No routing axis, so the only failure is a name that does not exist."""
    try:
        get_llm(settings=settings)
    except ProviderConfigurationError as exc:
        problems = [CapabilityProblem("llm", settings.llm_provider, str(exc))]
    else:
        problems = []
    return problems


def _payment_problems(settings: Settings) -> list[CapabilityProblem]:
    """Names, then currencies — a typo would otherwise be reported as a gap.

    Building each provider also runs the ENV=prod guard, which is the check that
    stops a deployment from accepting callbacks signed with a published key.
    """
    if not settings.payment_providers:
        return [
            CapabilityProblem(
                "payments", "any channel", "PAYMENT_PROVIDERS is empty; nothing can take money"
            )
        ]

    problems = []
    named_wrongly = False
    for name in settings.payment_providers:
        try:
            build_payment_provider(name, settings=settings)
        except ProviderConfigurationError as exc:
            problems.append(CapabilityProblem("payments", name, str(exc)))
            named_wrongly = True

    if named_wrongly:
        # Currency coverage would be misleading while a channel is unbuildable.
        return problems

    for currency in settings.supported_currencies:
        try:
            provider_for_currency(currency, settings=settings)
        except ProviderConfigurationError as exc:
            problems.append(CapabilityProblem("payments", currency, str(exc)))
    return problems


def _pricing_problems(settings: Settings) -> list[CapabilityProblem]:
    """A currency we accept but have no price in is one nobody can pay in.

    The symptom without this check is a learner reaching checkout and being
    refused for a reason that is entirely ours — and only for the currency
    nobody tested with.
    """
    problems = []
    for currency in settings.supported_currencies:
        for plan in PURCHASABLE_PLANS:
            for period in BILLING_PERIODS:
                try:
                    price_for(plan, period, currency, settings=settings)
                except (UnpricedError, ValueError) as exc:
                    problems.append(
                        CapabilityProblem("pricing", f"{plan} {period} in {currency}", str(exc))
                    )
    return problems


def main() -> int:
    """Run the check against the real environment. Non-zero means do not deploy.

    Deploy scripts and CI call this before starting anything; D1 will also wire
    check_provider_configuration into the application's own lifespan so a bad
    configuration cannot get past boot either way.
    """
    settings = Settings()
    configure_logging(log_level=settings.log_level, json_output=settings.env != "dev")
    try:
        check_provider_configuration(settings)
    except ProviderConfigurationError as exc:
        print(str(exc), file=sys.stderr)  # this is a CLI
        return 1
    print("provider self-check passed")  # this is a CLI
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
