"""Chasing the payments whose callback never arrived.

``PaymentProvider.query_status`` is described in the normative interface as the
fallback for a lost callback, and until this job nothing called it: an order
whose webhook went missing sat at pending forever, the learner had paid, no
subscription existed, and nothing in the system was looking. This is money, so
it is tested against the same FakeProvider the rest of the suite runs on —
whose order-id markers steer query_status precisely so these branches are
reachable without a broken acquirer.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.core.db import create_engine, create_session_factory
from app.core.security import decode_access_token
from app.services.payments.fake import FAILED_MARKER, PENDING_MARKER
from app.workers.payments import reconcile_pending_payments
from tests.integration.conftest import Execute, Rows, auth_header, token_for

pytestmark = pytest.mark.integration


async def authenticated(client: httpx.AsyncClient, **kwargs: Any) -> dict[str, str]:
    return auth_header(await token_for(client, **kwargs))


def order(
    execute: Execute,
    *,
    user_id: uuid.UUID,
    order_id: str,
    minutes_ago: int = 60,
    provider: str = "fake",
    plan: str = "basic",
    status: str = "pending",
) -> str:
    """A checkout that was created and then went quiet."""
    execute(
        "INSERT INTO payments (user_id, order_id, provider, amount_minor, currency, "
        " currency_minor_units, status, raw_payload, created_at, updated_at) "
        "VALUES (:u, :o, :p, 199, 'USD', 2, :s, CAST(:raw AS jsonb), "
        "        now() - make_interval(mins => :m), now() - make_interval(mins => :m))",
        u=user_id,
        o=order_id,
        p=provider,
        s=status,
        m=minutes_ago,
        raw=f'{{"kruai_order": {{"plan": "{plan}", "period": "monthly"}}}}',
    )
    return order_id


async def reconcile(settings: Settings, **kwargs: Any) -> Any:
    engine = create_engine(settings)
    try:
        return await reconcile_pending_payments(
            session_factory=create_session_factory(engine), settings=settings, **kwargs
        )
    finally:
        await engine.dispose()


# --------------------------------------------------------------- the rescue


async def test_a_lost_callback_is_found_and_the_plan_is_granted(
    client: httpx.AsyncClient, settings: Settings, execute: Execute, rows: Rows
) -> None:
    """The learner paid. Nothing told us. This is what notices."""
    user_id = decode_access_token(await token_for(client), settings=settings)
    order(execute, user_id=user_id, order_id=f"kruai_{uuid.uuid4().hex}")

    report = await reconcile(settings)

    assert report.settled == 1
    assert rows("SELECT status FROM payments") == [("succeeded",)]
    assert rows("SELECT plan, status FROM subscriptions") == [("basic", "active")]


async def test_pro_reconciled_late_still_gets_its_minutes(
    client: httpx.AsyncClient, settings: Settings, execute: Execute, rows: Rows
) -> None:
    """Whatever noticed the payment, the same thing has to be granted."""
    user_id = decode_access_token(await token_for(client), settings=settings)
    order(execute, user_id=user_id, order_id=f"kruai_{uuid.uuid4().hex}", plan="pro")

    await reconcile(settings)

    assert rows("SELECT plan FROM subscriptions") == [("pro",)]


async def test_an_order_the_acquirer_says_failed_is_closed(
    client: httpx.AsyncClient, settings: Settings, execute: Execute, rows: Rows
) -> None:
    user_id = decode_access_token(await token_for(client), settings=settings)
    order(execute, user_id=user_id, order_id=f"kruai_{FAILED_MARKER}_{uuid.uuid4().hex}")

    report = await reconcile(settings)

    assert (report.failed, report.settled) == (1, 0)
    assert rows("SELECT status FROM payments") == [("failed",)]
    assert rows("SELECT count(*) FROM subscriptions") == [(0,)]


async def test_an_order_still_genuinely_pending_is_left_alone(
    client: httpx.AsyncClient, settings: Settings, execute: Execute, rows: Rows
) -> None:
    """Somebody who has the checkout page open has not failed to pay."""
    user_id = decode_access_token(await token_for(client), settings=settings)
    order(execute, user_id=user_id, order_id=f"kruai_{PENDING_MARKER}_{uuid.uuid4().hex}")

    report = await reconcile(settings)

    assert (report.settled, report.failed, report.abandoned) == (0, 0, 0)
    assert rows("SELECT status FROM payments") == [("pending",)]


async def test_a_fresh_order_is_not_chased(
    client: httpx.AsyncClient, settings: Settings, execute: Execute, rows: Rows
) -> None:
    """A callback in flight is not a lost callback."""
    user_id = decode_access_token(await token_for(client), settings=settings)
    order(execute, user_id=user_id, order_id=f"kruai_{uuid.uuid4().hex}", minutes_ago=1)

    report = await reconcile(settings)

    assert report.examined == 0
    assert rows("SELECT status FROM payments") == [("pending",)]


async def test_an_order_nobody_ever_paid_is_written_off(
    client: httpx.AsyncClient, settings: Settings, execute: Execute, rows: Rows
) -> None:
    """A checkout opened and walked away from is the common case. Leaving them
    pending forever makes "how many payments are stuck" unanswerable."""
    user_id = decode_access_token(await token_for(client), settings=settings)
    order(
        execute,
        user_id=user_id,
        order_id=f"kruai_{PENDING_MARKER}_{uuid.uuid4().hex}",
        minutes_ago=60 * 48,
    )

    report = await reconcile(settings)

    assert report.abandoned == 1
    assert rows("SELECT status FROM payments") == [("failed",)]


async def test_a_settled_order_is_not_settled_twice(
    client: httpx.AsyncClient, settings: Settings, execute: Execute, rows: Rows
) -> None:
    """The job races the callback it exists to compensate for, and has to lose
    that race without doing damage."""
    user_id = decode_access_token(await token_for(client), settings=settings)
    order(execute, user_id=user_id, order_id=f"kruai_{uuid.uuid4().hex}")

    await reconcile(settings)
    second = await reconcile(settings)

    assert second.settled == 0
    assert rows("SELECT count(*) FROM subscriptions") == [(1,)]


async def test_an_order_from_a_channel_we_no_longer_run_is_left_pending(
    client: httpx.AsyncClient, settings: Settings, execute: Execute, rows: Rows
) -> None:
    """Nobody can ask, so nobody can settle. Saying so beats guessing."""
    user_id = decode_access_token(await token_for(client), settings=settings)
    order(execute, user_id=user_id, order_id=f"kruai_{uuid.uuid4().hex}", provider="retired")

    report = await reconcile(settings)

    assert report.unreachable == 1
    assert rows("SELECT status FROM payments") == [("pending",)]


async def test_an_empty_ledger_of_orders_is_not_an_error(settings: Settings) -> None:
    report = await reconcile(settings)

    assert (report.examined, report.settled) == (0, 0)


async def test_several_orders_are_each_judged_on_their_own(
    client: httpx.AsyncClient, settings: Settings, execute: Execute, rows: Rows
) -> None:
    user_id = decode_access_token(await token_for(client), settings=settings)
    order(execute, user_id=user_id, order_id=f"kruai_paid_{uuid.uuid4().hex}")
    order(execute, user_id=user_id, order_id=f"kruai_{FAILED_MARKER}_{uuid.uuid4().hex}")
    order(execute, user_id=user_id, order_id=f"kruai_{PENDING_MARKER}_{uuid.uuid4().hex}")

    report = await reconcile(settings)

    assert (report.examined, report.settled, report.failed) == (3, 1, 1)
    assert sorted(row[0] for row in rows("SELECT status FROM payments")) == [
        "failed",
        "pending",
        "succeeded",
    ]


async def test_an_order_whose_details_are_unreadable_still_grants_something(
    client: httpx.AsyncClient,
    settings: Settings,
    execute: Execute,
    rows: Rows,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The learner paid. Giving them nothing because our own metadata is
    unreadable would be the worse of two failures — so they get the cheaper
    plan and the log says loudly that somebody has to look.
    """
    user_id = decode_access_token(await token_for(client), settings=settings)
    order_id = f"kruai_{uuid.uuid4().hex}"
    order(execute, user_id=user_id, order_id=order_id)
    execute(
        'UPDATE payments SET raw_payload = CAST(\'{"nothing": "useful"}\' AS jsonb) '
        "WHERE order_id = :o",
        o=order_id,
    )

    with caplog.at_level(logging.ERROR):
        report = await reconcile(settings)

    assert report.settled == 1
    assert rows("SELECT plan, status FROM subscriptions") == [("basic", "active")]
    assert "payments.order_details_unreadable" in caplog.text


async def test_a_reconciled_order_keeps_the_mandate_checkout_was_given(
    client: httpx.AsyncClient, settings: Settings, execute: Execute, rows: Rows
) -> None:
    """A lost callback must not quietly turn automatic renewal into manual.

    The mandate arrived at checkout; the callback is simply the usual way it
    comes back. Without keeping it, an order rescued by this job renews by
    reminder instead of by charge, and the learner never asked for that
    (D-066).
    """
    headers = auth_header(await token_for(client))
    created = await client.post(
        "/api/v1/payments/checkout", headers=headers, json={"plan": "basic"}
    )
    execute("UPDATE payments SET created_at = now() - interval '2 hours'")

    await reconcile(settings)

    assert created.status_code == 201
    assert rows("SELECT renewal_mode, mandate_ref IS NOT NULL FROM subscriptions") == [
        ("auto", True)
    ]


async def test_the_webhook_still_wins_when_it_arrives_first(
    client: httpx.AsyncClient, settings: Settings, execute: Execute, rows: Rows
) -> None:
    """A settled order is invisible to the job: it only looks at pending."""
    user_id = decode_access_token(await token_for(client), settings=settings)
    order(
        execute,
        user_id=user_id,
        order_id=f"kruai_{uuid.uuid4().hex}",
        status="succeeded",
    )

    report = await reconcile(settings)

    assert report.examined == 0
    assert rows("SELECT count(*) FROM subscriptions") == [(0,)]


# ------------------------------------------------------- what the call cost


async def test_every_order_chased_leaves_a_ledger_row(
    client: httpx.AsyncClient, settings: Settings, execute: Execute, rows: Rows
) -> None:
    """Asking the acquirer is an external call, recorded like any other (D-075).

    This is the one that pays off: reconciliation runs on a timer against a
    channel nobody is watching, and the row is how "we asked them two thousand
    times last month" becomes visible before the invoice says so.
    """
    user_id = decode_access_token(await token_for(client), settings=settings)
    for _ in range(3):
        order(execute, user_id=user_id, order_id=f"kruai_{uuid.uuid4().hex}")

    await reconcile(settings)

    assert rows("SELECT count(*), sum(cost_usd_cents_est) FROM cost_ledger") == [(3, 0)]
    assert rows("SELECT DISTINCT ref, unit FROM cost_ledger") == [("payment", "calls")]


async def test_the_ledger_row_names_the_learner_the_order_belonged_to(
    client: httpx.AsyncClient, settings: Settings, execute: Execute, rows: Rows
) -> None:
    """Zero cost, but not anonymous: which learner an acquirer call was about
    is exactly what a support question asks."""
    user_id = decode_access_token(await token_for(client), settings=settings)
    order(execute, user_id=user_id, order_id=f"kruai_{uuid.uuid4().hex}")

    await reconcile(settings)

    assert rows("SELECT user_id FROM cost_ledger") == [(user_id,)]
