"""Taking a payment and granting what it bought (BACKLOG D8a).

The acceptance, in order: a fake payment completes and opens a subscription; a
callback whose signature does not verify opens nothing; and both currencies are
handled without either being read as the other.

The signature is the whole security boundary on the webhook — it is
unauthenticated by necessity, because an acquirer has no token of ours — so
several tests here are forgeries of one kind or another.
"""

from __future__ import annotations

import datetime
import json
import uuid
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.core.security import decode_access_token
from app.services.entitlements import is_over_threshold
from app.services.payments.fake import SIGNATURE_HEADER, sign_payload
from app.services.provider_errors import ProviderConfigurationError
from tests.integration.conftest import Execute, Rows, auth_header, client_for, token_for

pytestmark = pytest.mark.integration

CHECKOUT_URL = "/api/v1/payments/checkout"
WEBHOOK_URL = "/api/v1/payments/webhook/fake"


async def authenticated(client: httpx.AsyncClient, **kwargs: Any) -> dict[str, str]:
    return auth_header(await token_for(client, **kwargs))


async def checkout(
    client: httpx.AsyncClient, *, headers: dict[str, str], **body: Any
) -> httpx.Response:
    return await client.post(CHECKOUT_URL, headers=headers, json={"plan": "basic", **body})


def callback_body(order_id: str, *, status: str = "succeeded", **extra: Any) -> bytes:
    payload = {
        "order_id": order_id,
        "status": status,
        "provider_ref": f"fakeref_{order_id[-8:]}",
        **extra,
    }
    return json.dumps(payload).encode("utf-8")


async def deliver(
    client: httpx.AsyncClient, body: bytes, *, signature: str | None = None, url: str = WEBHOOK_URL
) -> httpx.Response:
    return await client.post(
        url,
        content=body,
        headers={
            "content-type": "application/json",
            SIGNATURE_HEADER: signature if signature is not None else sign_payload(body),
        },
    )


async def pay_for(client: httpx.AsyncClient, *, headers: dict[str, str], **body: Any) -> str:
    """Check out and settle it, the way a learner and an acquirer would."""
    created = await checkout(client, headers=headers, **body)
    assert created.status_code == 201, created.text
    order_id: str = created.json()["order_id"]
    settled = await deliver(client, callback_body(order_id, mandate_ref="fakemandate_1"))
    assert settled.status_code == 200
    return order_id


# ------------------------------------------------------------------ checkout


async def test_checkout_prices_the_plan_and_records_the_order(
    client: httpx.AsyncClient, rows: Rows
) -> None:
    headers = await authenticated(client)

    response = await checkout(client, headers=headers, plan="basic", period="monthly")

    assert response.status_code == 201
    body = response.json()
    assert body["amount_minor"] == 199
    assert body["currency"] == "USD"
    assert body["currency_minor_units"] == 2
    assert body["amount_display"] == "$1.99"
    assert body["checkout_url"] == f"fake://pay/{body['order_id']}"

    stored = rows(
        "SELECT order_id, amount_minor, currency, currency_minor_units, status FROM payments"
    )
    assert stored == [(body["order_id"], 199, "USD", 2, "pending")]


async def test_the_order_number_carries_nothing_about_the_learner(
    client: httpx.AsyncClient, settings: Settings
) -> None:
    """It travels through an acquirer's systems and their logs."""
    headers = await authenticated(client)
    user_id = decode_access_token(await token_for(client), settings=settings)

    order_id = (await checkout(client, headers=headers)).json()["order_id"]

    assert str(user_id) not in order_id
    assert order_id.startswith("kruai_")


async def test_a_yearly_order_costs_the_yearly_price(client: httpx.AsyncClient) -> None:
    headers = await authenticated(client)

    body = (await checkout(client, headers=headers, plan="pro", period="yearly")).json()

    assert body["amount_minor"] == 5400
    assert body["amount_display"] == "$54.00"


async def test_riel_is_priced_in_riel(settings: Settings, rows: Rows) -> None:
    """KHR has no decimal places, so 8000 is ៛8000 — not eighty riel."""
    # Every purchasable combination needs a riel price, or the startup
    # self-check refuses to boot — which is the point of it: a currency we
    # accept but cannot price is one nobody can pay in.
    dual = settings.model_copy(
        update={
            "supported_currencies": ("USD", "KHR"),
            "price_basic_monthly": "USD:199,KHR:8000",
            "price_basic_yearly": "USD:1800,KHR:72000",
            "price_pro_monthly": "USD:599,KHR:24000",
            "price_pro_yearly": "USD:5400,KHR:216000",
        }
    )

    async with client_for(dual) as client:
        headers = await authenticated(client)
        body = (await checkout(client, headers=headers, currency="KHR")).json()

    assert body["amount_minor"] == 8000
    assert body["currency_minor_units"] == 0
    assert body["amount_display"] == "៛8,000"
    assert body["qr_payload"] is not None, "KHQR is the riel-side checkout shape"
    assert rows("SELECT amount_minor, currency_minor_units FROM payments") == [(8000, 0)]


async def test_a_currency_we_accept_but_cannot_price_refuses_to_boot(
    settings: Settings,
) -> None:
    """The self-check found this while these tests were being written: riel was
    added to SUPPORTED_CURRENCIES with only one of the four prices filled in,
    and the application would not start. Without it the symptom is a learner
    reaching checkout and being refused for a reason that is entirely ours —
    and only in the currency nobody tested with.
    """
    half_priced = settings.model_copy(
        update={
            "supported_currencies": ("USD", "KHR"),
            "price_basic_monthly": "USD:199,KHR:8000",
        }
    )

    with pytest.raises(ProviderConfigurationError, match="pricing"):
        async with client_for(half_priced):
            pass  # pragma: no cover - the lifespan raises before this runs


async def test_free_cannot_be_checked_out(client: httpx.AsyncClient, rows: Rows) -> None:
    headers = await authenticated(client)

    response = await checkout(client, headers=headers, plan="free")

    assert response.status_code == 400
    assert response.json()["code"] == "payment.refused"
    assert rows("SELECT count(*) FROM payments") == [(0,)]


async def test_a_currency_we_do_not_accept_is_refused(client: httpx.AsyncClient) -> None:
    headers = await authenticated(client)

    response = await checkout(client, headers=headers, currency="KHR")

    assert response.status_code == 400


async def test_a_priced_currency_we_have_not_switched_on_is_still_refused(
    settings: Settings, rows: Rows
) -> None:
    """SUPPORTED_CURRENCIES is the switch; the price list is preparation.

    An operator who has filled in riel prices ahead of launching in riel has
    not launched in riel. Without this gate a client could ask for a currency
    nobody has decided to accept and simply pay in it — and the refusal it
    would otherwise hit ("no KHR price") is the same 400, which is why the
    mutation that deleted the check survived until this test existed.
    """
    prepared = settings.model_copy(
        update={
            "supported_currencies": ("USD",),
            "price_basic_monthly": "USD:199,KHR:8000",
            "price_basic_yearly": "USD:1800,KHR:72000",
            "price_pro_monthly": "USD:599,KHR:24000",
            "price_pro_yearly": "USD:5400,KHR:216000",
        }
    )

    async with client_for(prepared) as client:
        headers = await authenticated(client)
        response = await checkout(client, headers=headers, currency="KHR")

    assert response.status_code == 400
    assert rows("SELECT count(*) FROM payments") == [(0,)], "nothing was even recorded"


async def test_checkout_needs_a_token(client: httpx.AsyncClient) -> None:
    assert (await client.post(CHECKOUT_URL, json={"plan": "basic"})).status_code == 401


async def test_a_learner_cannot_buy_a_second_subscription(
    client: httpx.AsyncClient, rows: Rows
) -> None:
    """Both would renew forever: two charges a month for one account, and
    nothing in the system would ever notice (D-065)."""
    headers = await authenticated(client)
    await pay_for(client, headers=headers, plan="basic")

    response = await checkout(client, headers=headers, plan="pro", period="yearly")

    assert response.status_code == 409
    assert response.json()["code"] == "payment.already_subscribed", (
        "its own code, so a client can say 'you already have a plan' and so the "
        "upgrade flow in BACKLOG D11 has a named branch to replace"
    )
    assert rows("SELECT count(*) FROM subscriptions") == [(1,)]
    assert rows("SELECT count(*) FROM payments") == [(1,)], "the second order was never recorded"


async def test_a_learner_in_grace_cannot_start_a_second_subscription(
    client: httpx.AsyncClient, execute: Execute, rows: Rows
) -> None:
    """Grace still entitles, so it is still a live subscription."""
    headers = await authenticated(client)
    await pay_for(client, headers=headers, plan="basic")
    execute("UPDATE subscriptions SET status = 'grace', grace_until = now() + interval '2 days'")

    response = await checkout(client, headers=headers, plan="basic")

    assert response.status_code == 409
    assert rows("SELECT count(*) FROM subscriptions") == [(1,)]


async def test_a_learner_whose_subscription_expired_can_buy_again(
    client: httpx.AsyncClient, execute: Execute, rows: Rows
) -> None:
    """Refusing them would be refusing the renewal the manual path exists for."""
    headers = await authenticated(client)
    await pay_for(client, headers=headers, plan="basic")
    execute("UPDATE subscriptions SET status = 'expired'")

    response = await checkout(client, headers=headers, plan="basic")

    assert response.status_code == 201
    assert rows("SELECT count(*) FROM payments") == [(2,)]


# ------------------------------------------------------------------ webhook


async def test_a_verified_callback_opens_the_subscription(
    client: httpx.AsyncClient, rows: Rows, settings: Settings
) -> None:
    """The D8a acceptance: a fake payment completes and the plan is granted."""
    headers = await authenticated(client)
    user_id = decode_access_token(await token_for(client), settings=settings)

    await pay_for(client, headers=headers, plan="basic", period="monthly")

    assert rows("SELECT status FROM payments") == [("succeeded",)]
    subscription = rows(
        "SELECT user_id, plan, status, renewal_mode, period_end > now() FROM subscriptions"
    )
    assert subscription == [(user_id, "basic", "active", "auto", True)]


async def test_the_plan_takes_effect_immediately(client: httpx.AsyncClient) -> None:
    """current_plan reads subscriptions, so the paywall lifts on the same call."""
    headers = await authenticated(client)

    before = (await client.get("/api/v1/me/entitlements", headers=headers)).json()
    await pay_for(client, headers=headers, plan="basic")
    after = (await client.get("/api/v1/me/entitlements", headers=headers)).json()

    assert before["plan"] == "free"
    assert before["attempts"]["limit"] == 3
    assert after["plan"] == "basic"
    assert after["attempts"]["limit"] is None


async def test_a_forged_signature_grants_nothing(client: httpx.AsyncClient, rows: Rows) -> None:
    """ARCHITECTURE section 5: refuse, never "let through and reconcile later"."""
    headers = await authenticated(client)
    order_id = (await checkout(client, headers=headers)).json()["order_id"]

    response = await deliver(client, callback_body(order_id), signature="0" * 64)

    assert response.status_code == 400
    assert response.json()["code"] == "payment.refused"
    assert rows("SELECT status FROM payments") == [("pending",)]
    assert rows("SELECT count(*) FROM subscriptions") == [(0,)]


async def test_a_callback_with_no_signature_at_all_grants_nothing(
    client: httpx.AsyncClient, rows: Rows
) -> None:
    headers = await authenticated(client)
    order_id = (await checkout(client, headers=headers)).json()["order_id"]

    response = await client.post(
        WEBHOOK_URL, content=callback_body(order_id), headers={"content-type": "application/json"}
    )

    assert response.status_code == 400
    assert rows("SELECT count(*) FROM subscriptions") == [(0,)]


async def test_a_tampered_body_grants_nothing(client: httpx.AsyncClient, rows: Rows) -> None:
    """The classic: keep the signature, change what it vouches for."""
    headers = await authenticated(client)
    order_id = (await checkout(client, headers=headers)).json()["order_id"]
    honest = callback_body(order_id, status="failed")

    response = await deliver(
        client, callback_body(order_id, status="succeeded"), signature=sign_payload(honest)
    )

    assert response.status_code == 400
    assert rows("SELECT count(*) FROM subscriptions") == [(0,)]


async def test_a_callback_for_an_unknown_order_grants_nothing(
    client: httpx.AsyncClient, rows: Rows
) -> None:
    """Signed, well-formed, and about an order that does not exist."""
    response = await deliver(client, callback_body("kruai_" + uuid.uuid4().hex))

    assert response.status_code == 200, "nothing to retry, so the acquirer should stop"
    assert rows("SELECT count(*) FROM subscriptions") == [(0,)]


async def test_a_failed_payment_opens_nothing(client: httpx.AsyncClient, rows: Rows) -> None:
    headers = await authenticated(client)
    order_id = (await checkout(client, headers=headers)).json()["order_id"]

    response = await deliver(client, callback_body(order_id, status="failed"))

    assert response.status_code == 200
    assert rows("SELECT count(*) FROM subscriptions") == [(0,)]
    assert rows("SELECT status FROM payments") == [("pending",)]


async def test_a_retried_callback_does_not_grant_a_second_period(
    client: httpx.AsyncClient, rows: Rows
) -> None:
    """Every acquirer retries. Two subscriptions from one payment is the bug."""
    headers = await authenticated(client)
    order_id = (await checkout(client, headers=headers)).json()["order_id"]
    body = callback_body(order_id, mandate_ref="fakemandate_1")

    first = await deliver(client, body)
    second = await deliver(client, body)
    third = await deliver(client, body)

    assert (first.status_code, second.status_code, third.status_code) == (200, 200, 200)
    assert rows("SELECT count(*) FROM subscriptions") == [(1,)]
    assert rows("SELECT count(*) FROM payments WHERE status = 'succeeded'") == [(1,)]


async def test_an_unknown_provider_is_refused(client: httpx.AsyncClient) -> None:
    response = await deliver(
        client, callback_body("kruai_x"), url="/api/v1/payments/webhook/definitely-not-enabled"
    )

    assert response.status_code == 400


async def test_a_callback_naming_a_different_amount_grants_nothing(
    client: httpx.AsyncClient, rows: Rows
) -> None:
    """Correctly signed, and about an amount the order never asked for.

    The signature proves who sent it, not that they sent the right thing.
    Partial payments and adjusted amounts are real, and a year of Pro against
    one cent is not a rounding error (D-067).
    """
    headers = await authenticated(client)
    order_id = (await checkout(client, headers=headers, plan="pro", period="yearly")).json()[
        "order_id"
    ]

    response = await deliver(
        client,
        callback_body(order_id, amount_minor=1, currency="USD", currency_minor_units=2),
    )

    assert response.status_code == 200, "nothing to retry; the amount will not change"
    assert rows("SELECT count(*) FROM subscriptions") == [(0,)]
    assert rows("SELECT status FROM payments") == [("pending",)]


async def test_a_callback_naming_the_right_amount_settles(
    client: httpx.AsyncClient, rows: Rows
) -> None:
    headers = await authenticated(client)
    order_id = (await checkout(client, headers=headers, plan="pro", period="yearly")).json()[
        "order_id"
    ]

    await deliver(
        client,
        callback_body(order_id, amount_minor=5400, currency="USD", currency_minor_units=2),
    )

    assert rows("SELECT plan FROM subscriptions") == [("pro",)]


async def test_a_callback_naming_a_different_currency_grants_nothing(
    client: httpx.AsyncClient, rows: Rows
) -> None:
    """199 riel and 199 cents are the same integer and nothing like the same
    money — the exact confusion amount_minor exists to prevent."""
    headers = await authenticated(client)
    order_id = (await checkout(client, headers=headers, plan="basic")).json()["order_id"]

    await deliver(
        client,
        callback_body(order_id, amount_minor=199, currency="KHR", currency_minor_units=0),
    )

    assert rows("SELECT count(*) FROM subscriptions") == [(0,)]


async def test_a_silent_acquirer_is_still_believed(client: httpx.AsyncClient, rows: Rows) -> None:
    """Plenty of callbacks carry no amount. Refusing those would break
    settlement for a channel that is behaving."""
    headers = await authenticated(client)
    order_id = (await checkout(client, headers=headers, plan="basic")).json()["order_id"]

    await deliver(client, callback_body(order_id))

    assert rows("SELECT plan FROM subscriptions") == [("basic",)]


async def test_the_acquirer_payload_is_kept_beside_our_order(
    client: httpx.AsyncClient, rows: Rows
) -> None:
    """Reconciliation later wants both, and neither is the other's."""
    headers = await authenticated(client)
    order_id = await pay_for(client, headers=headers, plan="pro", period="yearly")

    stored = rows("SELECT raw_payload FROM payments WHERE order_id = :o", o=order_id)[0][0]

    assert stored["kruai_order"] == {"plan": "pro", "period": "yearly"}
    assert stored["provider_callback"]["order_id"] == order_id


# ------------------------------------------------------- what a plan includes


async def test_pro_arrives_with_its_realtime_balance(
    client: httpx.AsyncClient, rows: Rows, settings: Settings
) -> None:
    """The minutes are bought with the plan; they are a balance, not a daily
    counter, so they are granted rather than reset (C4)."""
    headers = await authenticated(client)

    await pay_for(client, headers=headers, plan="pro")

    assert rows("SELECT realtime_seconds_remaining FROM entitlements") == [
        (settings.limit_pro_realtime_seconds_monthly,)
    ]


async def test_basic_brings_no_realtime_minutes(client: httpx.AsyncClient, rows: Rows) -> None:
    """PRD 4.3: realtime is Pro only."""
    headers = await authenticated(client)

    await pay_for(client, headers=headers, plan="basic")

    assert rows("SELECT realtime_seconds_remaining FROM entitlements") == [(0,)]


async def test_a_channel_that_cannot_charge_again_lands_in_manual(
    settings: Settings, rows: Rows
) -> None:
    """ARCHITECTURE 3.4: the renewal path is decided by what the channel can
    do, never assumed. fake_manual is the stand-in for Telegram Stars."""
    manual_only = settings.model_copy(update={"payment_providers": ("fake_manual",)})

    async with client_for(manual_only) as client:
        headers = await authenticated(client)
        created = await client.post(
            CHECKOUT_URL, headers=headers, json={"plan": "basic", "provider": "fake_manual"}
        )
        order_id = created.json()["order_id"]
        await deliver(
            client,
            callback_body(order_id),
            url="/api/v1/payments/webhook/fake_manual",
        )

    assert rows("SELECT renewal_mode, mandate_ref, next_charge_at FROM subscriptions") == [
        ("manual", None, None)
    ]


async def test_an_auto_subscription_carries_the_mandate_and_the_next_charge(
    client: httpx.AsyncClient, rows: Rows
) -> None:
    """The database refuses renewal_mode='auto' without a mandate, and D8b
    drives next_charge_at — both have to be right from the first row."""
    headers = await authenticated(client)

    await pay_for(client, headers=headers, plan="basic", period="monthly")

    mode, mandate, next_charge, ends = rows(
        "SELECT renewal_mode, mandate_ref, next_charge_at, period_end FROM subscriptions"
    )[0]
    assert mode == "auto"
    assert mandate == "fakemandate_1"
    assert next_charge == ends


async def test_a_monthly_period_ends_on_the_calendar(client: httpx.AsyncClient, rows: Rows) -> None:
    headers = await authenticated(client)

    await pay_for(client, headers=headers, plan="basic", period="monthly")

    start, ends = rows("SELECT period_start, period_end FROM subscriptions")[0]
    assert ends > start
    assert (ends - start) >= datetime.timedelta(days=28)
    assert (ends - start) <= datetime.timedelta(days=31)


async def test_a_yearly_period_is_a_year(client: httpx.AsyncClient, rows: Rows) -> None:
    headers = await authenticated(client)

    await pay_for(client, headers=headers, plan="basic", period="yearly")

    start, ends = rows("SELECT period_start, period_end FROM subscriptions")[0]
    assert (ends - start) >= datetime.timedelta(days=365)


# ------------------------------------------------------- what the call cost


async def test_asking_the_acquirer_for_a_checkout_leaves_a_ledger_row(
    client: httpx.AsyncClient, rows: Rows
) -> None:
    """CLAUDE.md section 8: a call nobody recorded did not happen (D-075).

    Talking to an acquirer is an external call like any other, and the point of
    the row is not the money -- it is being able to see, in one place, how
    often we asked, how often they answered, and which channel is flaky.
    """
    headers = await authenticated(client)

    await checkout(client, headers=headers, plan="basic", period="monthly")

    assert rows("SELECT provider, unit, quantity, ref FROM cost_ledger") == [
        ("fake", "calls", 1.0, "payment")
    ]


async def test_the_checkout_call_is_recorded_as_costing_nothing(
    client: httpx.AsyncClient, rows: Rows
) -> None:
    """Zero on purpose, and this is the test that says why.

    The acquirer's fee is a cut of the transaction, so charging it to the
    learner who paid it would mean that subscribing pushes someone closer to
    their own cost ceiling. A learner throttled for having paid us is not a
    rounding error, it is the guardrail working backwards.
    """
    headers = await authenticated(client)

    await checkout(client, headers=headers, plan="pro", period="yearly")

    assert rows("SELECT cost_usd_cents_est FROM cost_ledger") == [(0,)]


async def test_paying_does_not_move_a_learner_toward_the_throttle(
    client: httpx.AsyncClient, rows: Rows, settings: Settings
) -> None:
    """The behaviour the zero exists for, asserted end to end.

    Twenty checkouts is far more than any learner would make, and it still adds
    nothing to what this month has cost us on their behalf.
    """
    headers = await authenticated(client)

    for _ in range(20):
        await checkout(client, headers=headers, plan="basic", period="monthly")

    spend = rows("SELECT coalesce(sum(cost_usd_cents_est), 0) FROM cost_ledger")[0][0]
    assert rows("SELECT count(*) FROM cost_ledger") == [(20,)]
    assert spend == 0
    assert not is_over_threshold(spend, plan="free", settings=settings)
