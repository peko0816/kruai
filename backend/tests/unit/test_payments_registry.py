"""Payment routing differs from the other three: several channels can be live.

PAYMENT_PROVIDERS is a list because a user may pay by card, KHQR or Stars, and
the webhook dispatches on whichever called back. The prod guard matters most
here — this fake's signing key is published in its own source.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.config import Settings
from app.services.payments import (
    available_providers,
    build_payment_provider,
    enabled_providers,
    get_payment_provider,
    provider_for_currency,
)
from app.services.payments.base import PaymentProvider

_MINIMAL: dict[str, str] = {
    "DATABASE_URL": "postgresql+psycopg://u:p@localhost:5432/db",
    "REDIS_URL": "redis://localhost:6379/0",
    "TELEGRAM_BOT_TOKEN": "",
    "AZURE_SPEECH_KEY": "",
    "AZURE_SPEECH_REGION": "",
    "GOOGLE_APPLICATION_CREDENTIALS": "",
    "ELEVENLABS_API_KEY": "",
    "OPENAI_API_KEY": "",
    "PAYWAY_MERCHANT_ID": "",
    "PAYWAY_API_KEY": "",
    "PAYWAY_BASE_URL": "",
    "BAKONG_TOKEN": "",
    "JWT_SECRET": "",
}

#: 32 characters: Settings refuses anything shorter when ENV=prod
#: (RFC 7518 section 3.2), and several tests below build a production config.
PROD_JWT_SECRET = "a-production-length-signing-key-0"


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


def settings(**overrides: str) -> Settings:
    values: dict[str, Any] = {**_MINIMAL, **overrides}
    return Settings(_env_file=None, **values)


def production(**overrides: str) -> Settings:
    return settings(ENV="prod", JWT_SECRET=PROD_JWT_SECRET, TELEGRAM_BOT_TOKEN="bot", **overrides)


# ------------------------------------------------------------- default wiring


def test_default_configuration_enables_the_fake() -> None:
    providers = enabled_providers(settings=settings())
    assert [p.name for p in providers] == ["fake"]


def test_caller_only_sees_the_abstraction() -> None:
    assert isinstance(build_payment_provider("fake", settings=settings()), PaymentProvider)


def test_both_renewal_capabilities_are_registered() -> None:
    """ARCHITECTURE 3.4 requires both paths to work; D8b drives them from here."""
    assert set(available_providers()) == {"fake", "fake_manual"}


def test_multiple_channels_can_be_enabled_at_once() -> None:
    configured = settings(PAYMENT_PROVIDERS="fake,fake_manual")
    assert [p.name for p in enabled_providers(settings=configured)] == ["fake", "fake_manual"]


def test_the_two_registered_fakes_differ_in_capability() -> None:
    configured = settings(PAYMENT_PROVIDERS="fake,fake_manual")
    recurring, manual = enabled_providers(settings=configured)

    assert recurring.supports_recurring is True
    assert manual.supports_recurring is False


# ----------------------------------------------------------------- prod guard


@pytest.mark.parametrize("name", ["fake", "fake_manual"])
def test_a_test_provider_is_refused_in_production(name: str) -> None:
    """Its signing key is in its own source, so a prod deployment using it would
    accept a callback forged by anyone who can read this repository."""
    with pytest.raises(ValueError, match="published in its own source"):
        build_payment_provider(name, settings=production())


def test_the_prod_guard_names_the_fix() -> None:
    with pytest.raises(ValueError, match="PAYMENT_PROVIDERS"):
        build_payment_provider("fake", settings=production())


def test_the_guard_does_not_fire_outside_production() -> None:
    for env in ("dev", "staging"):
        assert build_payment_provider("fake", settings=settings(ENV=env)) is not None


# --------------------------------------------------------------- misconfigured


def test_unknown_provider_names_the_typo_and_the_alternatives() -> None:
    with pytest.raises(ValueError, match="unknown payment provider 'aba'"):
        build_payment_provider("aba", settings=settings())


def test_a_callback_for_a_disabled_channel_is_refused() -> None:
    """Processing one on trust would mean honouring a channel nobody turned on."""
    with pytest.raises(ValueError, match="is not enabled"):
        get_payment_provider("fake_manual", settings=settings(PAYMENT_PROVIDERS="fake"))


def test_an_enabled_channel_resolves() -> None:
    provider = get_payment_provider(
        "fake_manual", settings=settings(PAYMENT_PROVIDERS="fake_manual")
    )
    assert provider.name == "fake_manual"


# ------------------------------------------------------------ currency routing


def test_a_currency_routes_to_a_channel_that_settles_it() -> None:
    configured = settings(PAYMENT_PROVIDERS="fake_manual,fake", SUPPORTED_CURRENCIES="USD,KHR")

    assert provider_for_currency("USD", settings=configured).name == "fake_manual"
    # fake_manual settles USD only, so KHR falls through to the next channel.
    assert provider_for_currency("KHR", settings=configured).name == "fake"


def test_a_currency_nobody_settles_is_a_deployment_error() -> None:
    configured = settings(PAYMENT_PROVIDERS="fake_manual")
    with pytest.raises(ValueError, match="no enabled payment provider settles KHR"):
        provider_for_currency("KHR", settings=configured)
