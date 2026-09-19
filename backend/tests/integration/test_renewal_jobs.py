"""The two renewal paths, end to end (BACKLOG D8b).

ARCHITECTURE 3.4's acceptance in one file: an acquirer that can charge again
renews by itself, one that cannot gets the learner reminded, and both end in
grace and then expiry if nobody pays. The business layer is forbidden from
assuming either path, so the tests drive both against the same code — ``fake``
takes mandates, ``fake_manual`` cannot, which is the real split between ABA and
a wallet.

Time is passed in rather than waited for. Every job takes ``now``, so a month
passing is an argument.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.core.db import create_engine, create_session_factory
from app.core.security import decode_access_token
from app.services.payments.fake import FAILED_MARKER
from app.workers.subscriptions import (
    charge_due_subscriptions,
    lapse_finished_subscriptions,
    remind_expiring_subscriptions,
)
from tests.integration.conftest import Execute, Rows, token_for

pytestmark = pytest.mark.integration

NOW = datetime.datetime(2026, 5, 4, 9, 0, tzinfo=datetime.UTC)


@dataclass
class RecordingSender:
    """Stands in for Telegram. Records, and can be told to fail."""

    delivers: bool = True
    sent: list[tuple[int, str, str]] = field(default_factory=list)

    async def send(self, telegram_id: int, *, message_key: str, locale: str) -> bool:
        self.sent.append((telegram_id, message_key, locale))
        return self.delivers


async def learner(client: httpx.AsyncClient, settings: Settings, **kwargs: Any) -> uuid.UUID:
    return decode_access_token(await token_for(client, **kwargs), settings=settings)


def subscribe(
    execute: Execute,
    user_id: uuid.UUID,
    *,
    mode: str = "auto",
    plan: str = "basic",
    status: str = "active",
    period_end: datetime.datetime,
    mandate: str | None = "fakemandate_1",
    next_charge_at: datetime.datetime | None = None,
    grace_until: datetime.datetime | None = None,
    provider: str = "fake",
    reminded: datetime.datetime | None = None,
) -> None:
    execute(
        "INSERT INTO subscriptions (user_id, plan, status, renewal_mode, period_start, "
        " period_end, mandate_ref, next_charge_at, grace_until, payment_provider, "
        " reminder_sent_at) "
        "VALUES (:u, :plan, :status, :mode, :start, :end, :mandate, :charge, :grace, :p, :rem)",
        u=user_id,
        plan=plan,
        status=status,
        mode=mode,
        start=period_end - datetime.timedelta(days=30),
        end=period_end,
        mandate=mandate if mode == "auto" else None,
        charge=next_charge_at if mode == "auto" else None,
        grace=grace_until,
        p=provider,
        rem=reminded,
    )


def paid(
    execute: Execute,
    user_id: uuid.UUID,
    *,
    period: str = "monthly",
    currency: str = "USD",
    amount: int = 199,
    when: datetime.datetime | None = None,
) -> None:
    """The payment that opened the period; renewals read its terms back."""
    execute(
        "INSERT INTO payments (user_id, order_id, provider, amount_minor, currency, "
        " currency_minor_units, status, is_recurring_charge, raw_payload, created_at, updated_at) "
        "VALUES (:u, :o, 'fake', :a, :c, :units, 'succeeded', false, CAST(:raw AS jsonb), :t, :t)",
        u=user_id,
        o=f"kruai_{uuid.uuid4().hex}",
        a=amount,
        c=currency,
        units=2 if currency == "USD" else 0,
        raw=f'{{"kruai_order": {{"plan": "basic", "period": "{period}"}}}}',
        t=when or (NOW - datetime.timedelta(days=30)),
    )


async def charge(settings: Settings, *, now: datetime.datetime = NOW) -> Any:
    engine = create_engine(settings)
    try:
        return await charge_due_subscriptions(
            session_factory=create_session_factory(engine), settings=settings, now=now
        )
    finally:
        await engine.dispose()


async def remind(
    settings: Settings, sender: RecordingSender, *, now: datetime.datetime = NOW
) -> Any:
    engine = create_engine(settings)
    try:
        return await remind_expiring_subscriptions(
            session_factory=create_session_factory(engine),
            settings=settings,
            sender=sender,
            now=now,
        )
    finally:
        await engine.dispose()


async def lapse(settings: Settings, *, now: datetime.datetime = NOW) -> Any:
    engine = create_engine(settings)
    try:
        return await lapse_finished_subscriptions(
            session_factory=create_session_factory(engine), settings=settings, now=now
        )
    finally:
        await engine.dispose()


# ---------------------------------------------------------------- auto path


async def test_a_due_subscription_renews_itself(
    client: httpx.AsyncClient, settings: Settings, execute: Execute, rows: Rows
) -> None:
    """The auto path's acceptance: supports_recurring=True charges again."""
    user_id = await learner(client, settings)
    paid(execute, user_id)
    subscribe(execute, user_id, period_end=NOW, next_charge_at=NOW)

    report = await charge(settings)

    assert (report.due, report.charged) == (1, 1)
    status, ends, next_charge = rows(
        "SELECT status, period_end, next_charge_at FROM subscriptions"
    )[0]
    assert status == "active"
    assert ends > NOW
    assert next_charge == ends


async def test_the_new_period_runs_from_the_old_one_s_end(
    client: httpx.AsyncClient, settings: Settings, execute: Execute, rows: Rows
) -> None:
    """A charge that lands late must not shorten what the learner bought, or
    walk their billing date backwards through the calendar."""
    user_id = await learner(client, settings)
    paid(execute, user_id)
    ended = NOW - datetime.timedelta(days=2)
    subscribe(execute, user_id, period_end=ended, next_charge_at=ended)

    await charge(settings)

    starts, ends = rows("SELECT period_start, period_end FROM subscriptions")[0]
    assert starts == ended
    assert ends == ended + datetime.timedelta(days=31)


async def test_a_renewal_is_charged_in_the_currency_they_pay_in(
    client: httpx.AsyncClient, settings: Settings, execute: Execute, rows: Rows
) -> None:
    """Riel has no decimal places; charging 199 of anything would be wrong."""
    dual = settings.model_copy(
        update={
            "supported_currencies": ("USD", "KHR"),
            "price_basic_monthly": "USD:199,KHR:8000",
            "price_basic_yearly": "USD:1800,KHR:72000",
            "price_pro_monthly": "USD:599,KHR:24000",
            "price_pro_yearly": "USD:5400,KHR:216000",
        }
    )
    user_id = await learner(client, dual)
    paid(execute, user_id, currency="KHR", amount=8000)
    subscribe(execute, user_id, period_end=NOW, next_charge_at=NOW)

    await charge(dual)

    charged = rows(
        "SELECT amount_minor, currency, currency_minor_units FROM payments "
        "WHERE is_recurring_charge = true"
    )
    assert charged == [(8000, "KHR", 0)]


async def test_a_yearly_subscription_renews_by_a_year(
    client: httpx.AsyncClient, settings: Settings, execute: Execute, rows: Rows
) -> None:
    user_id = await learner(client, settings)
    paid(execute, user_id, period="yearly")
    subscribe(execute, user_id, period_end=NOW, next_charge_at=NOW)

    await charge(settings)

    starts, ends = rows("SELECT period_start, period_end FROM subscriptions")[0]
    assert (ends - starts).days >= 365
    assert rows("SELECT amount_minor FROM payments WHERE is_recurring_charge = true") == [(1800,)]


async def test_every_charge_is_recorded_whether_it_worked_or_not(
    client: httpx.AsyncClient, settings: Settings, execute: Execute, rows: Rows
) -> None:
    user_id = await learner(client, settings)
    paid(execute, user_id)
    subscribe(execute, user_id, period_end=NOW, next_charge_at=NOW)

    await charge(settings)

    recorded = rows(
        "SELECT status, is_recurring_charge FROM payments WHERE is_recurring_charge = true"
    )
    assert recorded == [("succeeded", True)]


async def test_a_declined_charge_is_retried_rather_than_abandoned(
    client: httpx.AsyncClient, settings: Settings, execute: Execute, rows: Rows
) -> None:
    user_id = await learner(client, settings)
    paid(execute, user_id)
    subscribe(execute, user_id, period_end=NOW, next_charge_at=NOW, mandate=f"m_{FAILED_MARKER}")

    report = await charge(settings)

    assert (report.charged, report.retried) == (0, 1)
    status, next_charge = rows("SELECT status, next_charge_at FROM subscriptions")[0]
    assert status == "active", "benefits continue while we are still trying"
    assert next_charge == NOW + datetime.timedelta(days=1)
    assert rows("SELECT status FROM payments WHERE is_recurring_charge = true") == [("failed",)]


async def test_the_retries_run_out_and_the_subscription_reaches_grace(
    client: httpx.AsyncClient, settings: Settings, execute: Execute, rows: Rows
) -> None:
    """Three attempts with the shipped (1, 3) schedule, then grace — not a
    fourth charge, and not an immediate cut-off."""
    user_id = await learner(client, settings)
    paid(execute, user_id)
    ends = NOW
    subscribe(execute, user_id, period_end=ends, next_charge_at=ends, mandate=f"m_{FAILED_MARKER}")

    first = await charge(settings, now=ends)
    second = await charge(settings, now=ends + datetime.timedelta(days=1))
    third = await charge(settings, now=ends + datetime.timedelta(days=4))

    assert (first.retried, second.retried, third.graced) == (1, 1, 1)
    status, grace_until = rows("SELECT status, grace_until FROM subscriptions")[0]
    assert status == "grace"
    assert grace_until == ends + datetime.timedelta(days=settings.subscription_grace_days)
    assert rows("SELECT count(*) FROM payments WHERE is_recurring_charge = true") == [(3,)]


async def test_a_subscription_not_yet_due_is_left_alone(
    client: httpx.AsyncClient, settings: Settings, execute: Execute, rows: Rows
) -> None:
    user_id = await learner(client, settings)
    paid(execute, user_id)
    later = NOW + datetime.timedelta(days=5)
    subscribe(execute, user_id, period_end=later, next_charge_at=later)

    report = await charge(settings)

    assert report.due == 0
    assert rows("SELECT count(*) FROM payments WHERE is_recurring_charge = true") == [(0,)]


async def test_a_manual_subscription_is_never_charged(
    client: httpx.AsyncClient, settings: Settings, execute: Execute, rows: Rows
) -> None:
    """The business layer must not assume a channel can charge again."""
    user_id = await learner(client, settings)
    paid(execute, user_id)
    subscribe(execute, user_id, mode="manual", period_end=NOW)

    report = await charge(settings)

    assert report.due == 0
    assert rows("SELECT count(*) FROM payments WHERE is_recurring_charge = true") == [(0,)]


async def test_renewal_mode_decides_who_is_charged_not_the_timer(
    client: httpx.AsyncClient, settings: Settings, execute: Execute, rows: Rows
) -> None:
    """A manual subscription carrying a charge date must still not be charged.

    ARCHITECTURE 3.4's rule is that renewal_mode decides. Relying on
    next_charge_at being null for manual rows would be relying on a second
    invariant nothing enforces — the DDL permits the combination, and the
    mutation that dropped the renewal_mode filter survived until this test
    existed because every manual row in the suite happened to have no timer.
    """
    user_id = await learner(client, settings)
    paid(execute, user_id)
    subscribe(execute, user_id, mode="manual", period_end=NOW)
    execute("UPDATE subscriptions SET next_charge_at = :t", t=NOW)

    report = await charge(settings)

    assert report.due == 0
    assert rows("SELECT count(*) FROM payments WHERE is_recurring_charge = true") == [(0,)]
    assert rows("SELECT status FROM subscriptions") == [("active",)]


async def test_a_channel_that_can_no_longer_charge_lapses_gently(
    client: httpx.AsyncClient, settings: Settings, execute: Execute, rows: Rows
) -> None:
    """The acquirer was switched off. Grace leaves a manual renewal possible;
    failing loudly would only lose the learner."""
    user_id = await learner(client, settings)
    paid(execute, user_id)
    subscribe(execute, user_id, period_end=NOW, next_charge_at=NOW, provider="retired")

    report = await charge(settings)

    assert report.unchargeable == 1
    assert rows("SELECT status FROM subscriptions") == [("grace",)]


async def test_an_auto_subscription_cannot_exist_without_a_mandate(
    client: httpx.AsyncClient, settings: Settings, execute: Execute
) -> None:
    """Written to prove the job refuses to charge blind; it proved the state
    cannot be reached.

    ``auto_needs_mandate`` in DATA_MODEL.sql forbids the row outright, on both
    insert and update. The job still checks — it costs a comparison and the
    type says the column is nullable — but the guarantee is the database's, and
    saying so here is worth more than a test that could never have failed.
    """
    user_id = await learner(client, settings)
    subscribe(execute, user_id, period_end=NOW, next_charge_at=NOW)

    with pytest.raises(IntegrityError, match="auto_needs_mandate"):
        execute("UPDATE subscriptions SET mandate_ref = NULL")


# -------------------------------------------------------------- manual path


async def test_a_manual_subscription_is_reminded_before_it_ends(
    client: httpx.AsyncClient, settings: Settings, execute: Execute, rows: Rows
) -> None:
    """The manual path's acceptance: supports_recurring=False gets a nudge."""
    user_id = await learner(client, settings)
    subscribe(execute, user_id, mode="manual", period_end=NOW + datetime.timedelta(days=2))
    sender = RecordingSender()

    report = await remind(settings, sender)

    assert (report.due, report.sent) == (1, 1)
    assert sender.sent[0][1] == "bot.renewal_reminder"
    assert rows("SELECT reminder_sent_at IS NOT NULL FROM subscriptions") == [(True,)]


async def test_a_learner_is_reminded_once_and_not_again(
    client: httpx.AsyncClient, settings: Settings, execute: Execute
) -> None:
    """reminder_sent_at is the duplicate guard. Being told every hour until you
    pay is being nagged, not reminded."""
    user_id = await learner(client, settings)
    subscribe(execute, user_id, mode="manual", period_end=NOW + datetime.timedelta(days=2))
    sender = RecordingSender()

    await remind(settings, sender)
    await remind(settings, sender, now=NOW + datetime.timedelta(hours=1))
    await remind(settings, sender, now=NOW + datetime.timedelta(hours=2))

    assert len(sender.sent) == 1


async def test_a_reminder_that_did_not_arrive_is_not_recorded_as_sent(
    client: httpx.AsyncClient, settings: Settings, execute: Execute, rows: Rows
) -> None:
    """The one failure this job must not produce: a learner whose plan simply
    disappears because a message we never delivered was written down as sent."""
    user_id = await learner(client, settings)
    subscribe(execute, user_id, mode="manual", period_end=NOW + datetime.timedelta(days=2))
    sender = RecordingSender(delivers=False)

    report = await remind(settings, sender)

    assert (report.sent, report.undelivered) == (0, 1)
    assert rows("SELECT reminder_sent_at FROM subscriptions") == [(None,)]

    # And the next run tries again.
    working = RecordingSender()
    assert (await remind(settings, working)).sent == 1


async def test_a_subscription_far_from_expiry_is_not_reminded(
    client: httpx.AsyncClient, settings: Settings, execute: Execute
) -> None:
    user_id = await learner(client, settings)
    subscribe(execute, user_id, mode="manual", period_end=NOW + datetime.timedelta(days=20))
    sender = RecordingSender()

    assert (await remind(settings, sender)).due == 0


async def test_an_auto_subscription_is_not_reminded(
    client: httpx.AsyncClient, settings: Settings, execute: Execute
) -> None:
    """It renews itself; a reminder would be asking for money twice."""
    user_id = await learner(client, settings)
    subscribe(
        execute,
        user_id,
        period_end=NOW + datetime.timedelta(days=2),
        next_charge_at=NOW + datetime.timedelta(days=2),
    )
    sender = RecordingSender()

    assert (await remind(settings, sender)).due == 0


async def test_the_reminder_is_in_the_learner_s_own_language(
    client: httpx.AsyncClient, settings: Settings, execute: Execute
) -> None:
    user_id = await learner(client, settings)
    execute("UPDATE users SET locale = 'zh' WHERE id = :u", u=user_id)
    subscribe(execute, user_id, mode="manual", period_end=NOW + datetime.timedelta(days=1))
    sender = RecordingSender()

    await remind(settings, sender)

    assert sender.sent[0][2] == "zh"


# ------------------------------------------------- where both paths end up


async def test_a_manual_period_that_ran_out_enters_grace(
    client: httpx.AsyncClient, settings: Settings, execute: Execute, rows: Rows
) -> None:
    user_id = await learner(client, settings)
    ended = NOW - datetime.timedelta(hours=1)
    subscribe(execute, user_id, mode="manual", period_end=ended)

    report = await lapse(settings)

    assert report.graced == 1
    status, until = rows("SELECT status, grace_until FROM subscriptions")[0]
    assert status == "grace"
    assert until == ended + datetime.timedelta(days=settings.subscription_grace_days)


async def test_benefits_continue_through_grace(
    client: httpx.AsyncClient, settings: Settings, execute: Execute
) -> None:
    """ARCHITECTURE 3.4. A payment a day late does not take the lesson away."""
    user_id = await learner(client, settings)
    subscribe(execute, user_id, mode="manual", period_end=NOW - datetime.timedelta(hours=1))
    await lapse(settings)

    entitlements = await client.get(
        "/api/v1/me/entitlements",
        headers={"Authorization": f"Bearer {await token_for(client)}"},
    )

    assert entitlements.json()["plan"] == "basic"


async def test_grace_running_out_drops_the_learner_to_free(
    client: httpx.AsyncClient, settings: Settings, execute: Execute, rows: Rows
) -> None:
    """The D8b acceptance: grace expiring downgrades, and the paywall notices."""
    user_id = await learner(client, settings)
    subscribe(
        execute,
        user_id,
        mode="manual",
        status="grace",
        period_end=NOW - datetime.timedelta(days=5),
        grace_until=NOW - datetime.timedelta(hours=1),
    )

    report = await lapse(settings)

    assert report.expired == 1
    assert rows("SELECT status FROM subscriptions") == [("expired",)]

    entitlements = await client.get(
        "/api/v1/me/entitlements",
        headers={"Authorization": f"Bearer {await token_for(client)}"},
    )
    assert entitlements.json()["plan"] == "free"
    assert entitlements.json()["attempts"]["limit"] == settings.limit_free_daily_attempts


async def test_grace_that_has_not_run_out_is_left_alone(
    client: httpx.AsyncClient, settings: Settings, execute: Execute, rows: Rows
) -> None:
    user_id = await learner(client, settings)
    subscribe(
        execute,
        user_id,
        mode="manual",
        status="grace",
        period_end=NOW - datetime.timedelta(days=1),
        grace_until=NOW + datetime.timedelta(days=2),
    )

    report = await lapse(settings)

    assert report.expired == 0
    assert rows("SELECT status FROM subscriptions") == [("grace",)]


async def test_an_auto_subscription_with_a_charge_still_scheduled_is_not_lapsed(
    client: httpx.AsyncClient, settings: Settings, execute: Execute, rows: Rows
) -> None:
    """Its own job owns it. Cutting a learner off because a charge is running
    late is worse than letting the period run a day over."""
    user_id = await learner(client, settings)
    subscribe(
        execute,
        user_id,
        period_end=NOW - datetime.timedelta(hours=2),
        next_charge_at=NOW + datetime.timedelta(hours=1),
    )

    report = await lapse(settings)

    assert report.graced == 0
    assert rows("SELECT status FROM subscriptions") == [("active",)]


async def test_a_subscription_nothing_will_ever_renew_does_lapse(
    client: httpx.AsyncClient, settings: Settings, execute: Execute, rows: Rows
) -> None:
    """Auto, period over, and no charge scheduled: nothing is coming for it."""
    user_id = await learner(client, settings)
    subscribe(execute, user_id, period_end=NOW - datetime.timedelta(hours=2), next_charge_at=None)

    report = await lapse(settings)

    assert report.graced == 1
    assert rows("SELECT status FROM subscriptions") == [("grace",)]


async def test_one_pass_cannot_both_grant_grace_and_take_it_away(
    client: httpx.AsyncClient, settings: Settings, execute: Execute, rows: Rows
) -> None:
    """Expiry is judged before grace is granted, so a subscription that only
    just lapsed keeps the days it was promised."""
    user_id = await learner(client, settings)
    subscribe(execute, user_id, mode="manual", period_end=NOW - datetime.timedelta(days=30))

    report = await lapse(settings)

    assert (report.graced, report.expired) == (1, 0)
    assert rows("SELECT status FROM subscriptions") == [("grace",)]


async def test_a_renewed_subscription_can_be_reminded_again_next_period(
    client: httpx.AsyncClient, settings: Settings, execute: Execute, rows: Rows
) -> None:
    """reminder_sent_at is cleared by a renewal, or the second period would run
    out in silence."""
    user_id = await learner(client, settings)
    paid(execute, user_id)
    subscribe(
        execute,
        user_id,
        period_end=NOW,
        next_charge_at=NOW,
        reminded=NOW - datetime.timedelta(days=3),
    )

    await charge(settings)

    assert rows("SELECT reminder_sent_at FROM subscriptions") == [(None,)]


async def test_a_deleted_learner_is_not_chased(
    client: httpx.AsyncClient, settings: Settings, execute: Execute, rows: Rows
) -> None:
    """A soft-deleted account has no subscription to renew and nobody to remind."""
    user_id = await learner(client, settings)
    paid(execute, user_id)
    subscribe(execute, user_id, period_end=NOW, next_charge_at=NOW)
    execute("UPDATE users SET deleted_at = now() WHERE id = :u", u=user_id)

    charged = await charge(settings)
    lapsed = await lapse(settings)

    assert (charged.due, lapsed.graced) == (0, 0)
    assert rows("SELECT count(*) FROM payments WHERE is_recurring_charge = true") == [(0,)]
