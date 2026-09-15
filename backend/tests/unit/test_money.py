"""Money boundary cases.

CODING_STANDARDS section 3 makes these mandatory: zero, negative, and the
KHR-versus-USD split are exactly where a dual-currency system goes wrong. The
failure mode is silent — a KHR amount rendered with two decimal places looks
plausible and is off by 100x.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest

from app.core.money import (
    CURRENCY_SYMBOLS,
    MINOR_UNITS,
    format_amount,
    format_money,
    is_supported_currency,
    minor_units_for,
    validate_money,
)


@dataclass(frozen=True)
class FakeMoney:
    """Structural stand-in for payments.base.Money, which does not exist yet."""

    amount_minor: int
    currency: str
    currency_minor_units: int


def usd(amount_minor: int) -> FakeMoney:
    return FakeMoney(amount_minor, "USD", 2)


def khr(amount_minor: int) -> FakeMoney:
    return FakeMoney(amount_minor, "KHR", 0)


# ------------------------------------------------------------------- metadata


def test_every_currency_has_a_symbol() -> None:
    assert set(MINOR_UNITS) == set(CURRENCY_SYMBOLS)


def test_khr_has_no_decimal_places() -> None:
    """The premise of the whole amount_minor convention."""
    assert minor_units_for("KHR") == 0
    assert minor_units_for("USD") == 2


def test_unknown_currency_rejected() -> None:
    assert is_supported_currency("EUR") is False
    with pytest.raises(ValueError, match="unknown currency"):
        minor_units_for("EUR")


# ------------------------------------------------------------------ formatting


@pytest.mark.parametrize(
    ("amount_minor", "expected"),
    [
        (0, "$0.00"),
        (1, "$0.01"),
        (5, "$0.05"),
        (50, "$0.50"),
        (99, "$0.99"),
        (100, "$1.00"),
        (199, "$1.99"),
        (599, "$5.99"),
        (1800, "$18.00"),
        (123456, "$1,234.56"),
        (-199, "-$1.99"),
        (-1, "-$0.01"),
    ],
)
def test_format_usd(amount_minor: int, expected: str) -> None:
    assert format_money(usd(amount_minor)) == expected


@pytest.mark.parametrize(
    ("amount_minor", "expected"),
    [
        (0, "៛0"),
        (1, "៛1"),
        (100, "៛100"),
        (8000, "៛8,000"),
        (1000000, "៛1,000,000"),
        (-8000, "-៛8,000"),
    ],
)
def test_format_khr_never_shows_decimals(amount_minor: int, expected: str) -> None:
    assert format_money(khr(amount_minor)) == expected


def test_format_without_symbol() -> None:
    assert format_money(usd(199), with_symbol=False) == "1.99"
    assert format_money(khr(8000), with_symbol=False) == "8,000"


def test_negative_sign_precedes_symbol() -> None:
    """-$1.99, not $-1.99."""
    assert format_money(usd(-199)).startswith("-$")


def test_prd_prices_render_correctly() -> None:
    """Basic $1.99/mo, Pro $5.99/mo, annual $18 and $54 (PRD 4.3)."""
    assert format_money(usd(199)) == "$1.99"
    assert format_money(usd(599)) == "$5.99"
    assert format_money(usd(1800)) == "$18.00"
    assert format_money(usd(5400)) == "$54.00"


def test_currency_without_symbol_falls_back_to_code() -> None:
    assert format_amount(1234, "JPY", 0) == "1,234 JPY"


# ------------------------------------------------------------------ validation


def test_float_amount_rejected() -> None:
    money = FakeMoney(1.99, "USD", 2)  # type: ignore[arg-type]  # the bug under test
    with pytest.raises(TypeError, match="float money is a bug"):
        validate_money(money)


def test_bool_amount_rejected() -> None:
    """bool is a subtype of int, so mypy waves this through and only the
    runtime guard catches it. That is exactly why the guard exists."""
    with pytest.raises(TypeError, match="must be int"):
        validate_money(FakeMoney(True, "USD", 2))


def test_khr_declaring_two_minor_units_rejected() -> None:
    """The exact bug the amount_minor convention exists to prevent."""
    with pytest.raises(ValueError, match="KHR has 0 minor units"):
        validate_money(FakeMoney(8000, "KHR", 2))


def test_usd_declaring_zero_minor_units_rejected() -> None:
    with pytest.raises(ValueError, match="USD has 2 minor units"):
        validate_money(FakeMoney(199, "USD", 0))


def test_unknown_currency_in_money_rejected() -> None:
    with pytest.raises(ValueError, match="unknown currency"):
        validate_money(FakeMoney(100, "EUR", 2))


@pytest.mark.parametrize("amount_minor", [0, 1, -1, 2_147_483_647])
def test_valid_amounts_accepted(amount_minor: int) -> None:
    validate_money(usd(amount_minor))


def test_format_amount_rejects_negative_minor_units() -> None:
    with pytest.raises(ValueError, match="currency_minor_units"):
        format_amount(100, "USD", -1)


# -------------------------------------------------- normative interface contract


def _load_normative_payments_base() -> ModuleType:
    """Load interfaces/payments_base.py, the file B4 copies verbatim.

    Re-point this at app.services.payments.base once B4 lands.
    """
    path = Path(__file__).resolve().parents[3] / "interfaces" / "payments_base.py"
    name = "normative_payments_base"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Must be registered before exec: the module uses `from __future__ import
    # annotations`, so @dataclass resolves its string annotations by looking
    # itself up in sys.modules.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        del sys.modules[name]
        raise
    return module


def test_normative_money_satisfies_moneylike() -> None:
    """core.money deliberately defines no Money class of its own.

    It works against the MoneyLike protocol precisely so that the normative
    payments.base.Money satisfies it without core importing the adapter layer
    (docs/DECISIONS.md D-003). If that stops holding, this fails here rather
    than in B4.
    """
    money_cls = _load_normative_payments_base().Money

    assert format_money(money_cls(amount_minor=199, currency="USD", currency_minor_units=2)) == (
        "$1.99"
    )
    assert format_money(money_cls(amount_minor=8000, currency="KHR", currency_minor_units=0)) == (
        "៛8,000"
    )
