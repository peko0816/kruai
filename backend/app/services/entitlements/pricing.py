"""What a plan costs, and how long a period lasts.

Pure arithmetic over values. The prices themselves are configuration (PRD 4.3
sets them; R3 puts anything that can change on line into config), and this
module is the only place that turns "basic, monthly, USD" into an amount.

``Money`` comes from ``services/payments/base.py``, the normative file
CLAUDE.md section 4 forbids changing. Importing it here is the one adapter
import the domain layer is allowed (ARCHITECTURE section 1, and the layering
test enforces exactly that), and it is the reason ``core/money.py`` defines a
protocol rather than a second, incompatible money class (docs/DECISIONS.md
D-003).

**A period is calendar arithmetic, not a number of days.** A month is not 30
days and a year is not 365: billing someone on the 31st of January and again
30 days later would drift a subscription through the calendar, and every
statement after the first would fall on a different day than the one they
agreed to.
"""

from __future__ import annotations

import calendar
import datetime
from typing import Final, Literal

from app.core.config import Plan, Settings
from app.core.money import minor_units_for
from app.services.payments.base import Money

#: How long a subscription is bought for. PRD 4.3 offers both, with the yearly
#: price set about 25% below twelve months to improve cash flow and retention.
BillingPeriod = Literal["monthly", "yearly"]

BILLING_PERIODS: Final[tuple[BillingPeriod, ...]] = ("monthly", "yearly")

#: Plans that can be bought. 'free' is what everybody has without paying, so it
#: has no price and cannot be checked out.
PURCHASABLE_PLANS: Final[tuple[Plan, ...]] = ("basic", "pro")


class UnpricedError(LookupError):
    """No price for this plan, period and currency.

    A configuration gap rather than a business rule: a currency listed in
    SUPPORTED_CURRENCIES with no price is one nobody can actually pay in, and
    the startup self-check refuses to boot that deployment (BACKLOG B5).
    """


def parse_price_table(raw: str) -> dict[str, int]:
    """Turn ``"USD:199,KHR:8000"`` into ``{"USD": 199, "KHR": 8000}``.

    One configuration key per plan and period, carrying every currency, rather
    than a key per combination — four plans-times-periods times however many
    currencies is a lot of names to keep in step, and adding a currency should
    not mean adding four settings.

    Raises:
        ValueError: malformed, an unknown currency, a non-integer amount, or a
            price at or below zero. All of them are a deployment that cannot
            take money correctly, so none of them is allowed to start.
    """
    table: dict[str, int] = {}
    for entry in (part.strip() for part in raw.split(",") if part.strip()):
        currency, separator, amount = entry.partition(":")
        if not separator:
            raise ValueError(f"price entry {entry!r} is not CURRENCY:amount_minor")

        currency = currency.strip().upper()
        # Raises for a currency with no declared minor-unit count, which is the
        # one thing that would let a KHR price be stored as if it had cents.
        minor_units_for(currency)

        try:
            minor = int(amount.strip())
        except ValueError:
            raise ValueError(
                f"price for {currency} is {amount.strip()!r}; amounts are integers in the "
                "smallest unit of the currency, and float money is a bug"
            ) from None
        if minor <= 0:
            raise ValueError(f"price for {currency} is {minor}; a plan cannot cost nothing")
        if currency in table:
            raise ValueError(f"{currency} is priced twice in {raw!r}")
        table[currency] = minor

    return table


def price_for(plan: Plan, period: BillingPeriod, currency: str, *, settings: Settings) -> Money:
    """What this plan costs, as a Money the payment layer can take.

    Raises:
        UnpricedError: nothing priced for that combination.
        ValueError: the plan is not one that can be bought.
    """
    if plan not in PURCHASABLE_PLANS:
        raise ValueError(f"{plan!r} cannot be bought; purchasable plans are {PURCHASABLE_PLANS}")

    table = settings.price_table(plan, period)
    amount = table.get(currency.upper())
    if amount is None:
        raise UnpricedError(
            f"no {currency} price for {plan} {period}; priced currencies are {sorted(table)}"
        )
    return Money(
        amount_minor=amount,
        currency=currency.upper(),
        currency_minor_units=minor_units_for(currency.upper()),
    )


def period_end(start: datetime.datetime, period: BillingPeriod) -> datetime.datetime:
    """When a period bought at ``start`` runs out.

    Calendar arithmetic, clamped to the length of the target month: a
    subscription started on 31 January renews on 28 February and then on 31
    March, which is what a monthly plan means. Thirty days would walk the
    billing date backwards through the year.

    Raises:
        ValueError: a naive ``start``. Storing one in period_end would make an
            expiry mean a different instant depending on the session's timezone.
    """
    if start.tzinfo is None or start.tzinfo.utcoffset(start) is None:
        raise ValueError("start must be timezone-aware; period_end is a TIMESTAMPTZ")

    months = 1 if period == "monthly" else 12
    total = start.month - 1 + months
    year = start.year + total // 12
    month = total % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return start.replace(year=year, month=month, day=day)


def grace_until(period_ends: datetime.datetime, *, settings: Settings) -> datetime.datetime:
    """When benefits actually stop (ARCHITECTURE 3.4).

    Days rather than calendar months, because a grace window is a fixed
    courtesy rather than a billing term.
    """
    return period_ends + datetime.timedelta(days=settings.subscription_grace_days)


__all__ = [
    "BILLING_PERIODS",
    "PURCHASABLE_PLANS",
    "BillingPeriod",
    "UnpricedError",
    "grace_until",
    "parse_price_table",
    "period_end",
    "price_for",
]
