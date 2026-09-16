"""Selects payment providers by configuration.

Unlike the other three adapters, several can be live at once: PAYMENT_PROVIDERS
is a list because a user may pay by ABA card, KHQR, or Stars, and the webhook
route dispatches on which channel called back. So this resolves a set, not a
single provider, and routes by name and by currency rather than by language.

ENV=prod refuses to build a fake. FakePaymentProvider's signing key is published
in its source — it has to be, or no test could produce a valid signature — which
means a production deployment configured with it would accept a callback forged
by anyone who can read this repository, and grant the subscription it claims.
The other fakes are merely useless in production; this one is exploitable.
"""

from __future__ import annotations

from collections.abc import Callable

from app.core.config import Settings
from app.services.payments.base import PaymentProvider
from app.services.payments.fake import FakePaymentProvider
from app.services.provider_errors import ProviderConfigurationError

#: Two entries, one class. ARCHITECTURE 3.4 requires both renewal paths to work,
#: and the business layer must not assume either, so both capabilities are
#: always available to test against.
_FACTORIES: dict[str, Callable[[], PaymentProvider]] = {
    "fake": lambda: FakePaymentProvider(
        name="fake", supports_recurring=True, supported_currencies=("USD", "KHR")
    ),
    "fake_manual": lambda: FakePaymentProvider(
        name="fake_manual", supports_recurring=False, supported_currencies=("USD",)
    ),
}

#: Never valid in production. See the module docstring.
_TEST_ONLY_PROVIDERS = frozenset({"fake", "fake_manual"})


def available_providers() -> tuple[str, ...]:
    return tuple(sorted(_FACTORIES))


def build_payment_provider(name: str, *, settings: Settings) -> PaymentProvider:
    """Instantiate one provider by name.

    Raises:
        ValueError: no such implementation, or a test provider under ENV=prod.
    """
    if name in _TEST_ONLY_PROVIDERS and settings.env == "prod":
        raise ProviderConfigurationError(
            f"payment provider {name!r} signs with a key published in its own source; "
            "configuring it with ENV=prod would accept forged callbacks. Set "
            "PAYMENT_PROVIDERS to a real acquirer."
        )

    factory = _FACTORIES.get(name)
    if factory is None:
        raise ProviderConfigurationError(
            f"unknown payment provider {name!r}; implemented: {list(available_providers())}"
        )
    return factory()


def enabled_providers(*, settings: Settings) -> tuple[PaymentProvider, ...]:
    """Every provider PAYMENT_PROVIDERS turns on, in configured order."""
    return tuple(
        build_payment_provider(name, settings=settings) for name in settings.payment_providers
    )


def get_payment_provider(name: str, *, settings: Settings) -> PaymentProvider:
    """One enabled provider, for dispatching a webhook to the channel that called.

    Raises:
        ValueError: the name is not in PAYMENT_PROVIDERS. A callback arriving for
            a channel nobody enabled is not something to process on trust.
    """
    if name not in settings.payment_providers:
        raise ProviderConfigurationError(
            f"payment provider {name!r} is not enabled; "
            f"PAYMENT_PROVIDERS is {list(settings.payment_providers)}"
        )
    return build_payment_provider(name, settings=settings)


def provider_for_currency(currency: str, *, settings: Settings) -> PaymentProvider:
    """First enabled provider that can settle ``currency``.

    Raises:
        ValueError: nothing enabled settles it. Offering a price in a currency
            no channel can take is a deployment error, not a user-facing one.
    """
    for provider in enabled_providers(settings=settings):
        if currency in provider.supported_currencies:
            return provider

    raise ProviderConfigurationError(
        f"no enabled payment provider settles {currency}; "
        f"PAYMENT_PROVIDERS is {list(settings.payment_providers)}"
    )
