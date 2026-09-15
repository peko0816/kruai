"""Currency metadata and the only place 10**n conversions are allowed.

Cambodia runs a dual-currency economy: USD has 2 decimal places, KHR has 0.
That is why nothing here says "cents" — an ``amount_cents`` holding KHR is a
bug by construction (CODING_STANDARDS section 3). User-facing money is always
``amount_minor`` (integer, smallest unit of that currency) plus
``currency_minor_units``. Internal cost accounting is a different quantity and
keeps its own ``_usd_cents`` naming; it never passes through this module.

Deliberately no ``Money`` class here. ``interfaces/payments_base.py`` is a
normative file that defines ``Money`` and gets copied verbatim into
``services/payments/base.py`` (CLAUDE.md section 4). Defining a second
structurally-identical dataclass would give the codebase two incompatible money
types. Instead this module works against the ``MoneyLike`` protocol, which
``payments.base.Money`` satisfies structurally — and which keeps the dependency
pointing the right way, since core must not import from the adapter layer.
"""

from __future__ import annotations

from typing import Final, Protocol

#: Decimal places per currency. The authority for ``currency_minor_units``.
MINOR_UNITS: Final[dict[str, int]] = {
    "USD": 2,
    "KHR": 0,
}

#: Display symbols. KHR is U+17DB KHMER CURRENCY SYMBOL RIEL.
CURRENCY_SYMBOLS: Final[dict[str, str]] = {
    "USD": "$",
    "KHR": "៛",
}


class MoneyLike(Protocol):
    """Structural contract for a user-facing amount.

    ``payments.base.Money`` satisfies this without importing anything from here.
    """

    @property
    def amount_minor(self) -> int: ...

    @property
    def currency(self) -> str: ...

    @property
    def currency_minor_units(self) -> int: ...


def is_supported_currency(currency: str) -> bool:
    return currency in MINOR_UNITS


def minor_units_for(currency: str) -> int:
    """Decimal places for ``currency``.

    Raises:
        ValueError: unknown currency. Adding one means adding it to MINOR_UNITS
            and CURRENCY_SYMBOLS together.
    """
    try:
        return MINOR_UNITS[currency]
    except KeyError:
        raise ValueError(f"unknown currency {currency!r}; known: {sorted(MINOR_UNITS)}") from None


def validate_money(money: MoneyLike) -> None:
    """Assert a money value is well-formed.

    A broken invariant here is a programming error, so this raises native
    exceptions rather than AppError (CODING_STANDARDS section 5.1).

    Raises:
        TypeError: ``amount_minor`` is not an int (float money, or a bool
            sneaking through ``isinstance(True, int)``).
        ValueError: unknown currency, or ``currency_minor_units`` disagreeing
            with the currency — a KHR amount declaring 2 decimal places is the
            exact bug this project's money discipline exists to prevent.
    """
    amount = money.amount_minor
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise TypeError(
            f"amount_minor must be int, got {type(amount).__name__}; float money is a bug"
        )

    expected = minor_units_for(money.currency)
    if money.currency_minor_units != expected:
        raise ValueError(
            f"{money.currency} has {expected} minor units, "
            f"got currency_minor_units={money.currency_minor_units}"
        )


def format_amount(
    amount_minor: int,
    currency: str,
    currency_minor_units: int,
    *,
    with_symbol: bool = True,
) -> str:
    """Render an amount for display. Presentation layer only.

    Negative amounts (refunds, adjustments) render with the sign outside the
    symbol: ``-$1.99``.
    """
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise TypeError(
            f"amount_minor must be int, got {type(amount_minor).__name__}; float money is a bug"
        )
    if currency_minor_units < 0:
        raise ValueError(f"currency_minor_units must be >= 0, got {currency_minor_units}")

    sign = "-" if amount_minor < 0 else ""
    # abs() first: Python floor-divides negatives away from zero (-199 // 100 == -2).
    magnitude = abs(amount_minor)

    if currency_minor_units == 0:
        body = f"{magnitude:,}"
    else:
        scale = 10**currency_minor_units
        major, minor = divmod(magnitude, scale)
        body = f"{major:,}.{minor:0{currency_minor_units}d}"

    if not with_symbol:
        return f"{sign}{body}"

    symbol = CURRENCY_SYMBOLS.get(currency)
    if symbol is None:
        return f"{sign}{body} {currency}"
    return f"{sign}{symbol}{body}"


def format_money(money: MoneyLike, *, with_symbol: bool = True) -> str:
    """Render a MoneyLike for display, validating it first."""
    validate_money(money)
    return format_amount(
        money.amount_minor,
        money.currency,
        money.currency_minor_units,
        with_symbol=with_symbol,
    )
