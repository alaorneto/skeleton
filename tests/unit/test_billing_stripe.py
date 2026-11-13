"""Stripe adapter (#401) against recorded responses and a fixed signature vector. No network."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from skeleton.billing.providers.base import InvoiceData, PaymentState, WebhookRejected
from skeleton.billing.providers.stripe import PROVIDER as STRIPE
from tests.unit.billing_recorded import Account, recorder  # noqa: F401

ORG = UUID("11111111-1111-1111-1111-111111111111")
INV = UUID("22222222-2222-2222-2222-222222222222")
INVOICE = InvoiceData(ORG, INV, "2026-000007", "BRL", 4990, "Fatura 2026-000007", datetime(2026, 3, 1, tzinfo=UTC))
BODY = b'{"id":"evt_1","type":"payment_intent.succeeded","data":{"object":{"id":"pi_1"}}}'
VECTOR = "e487fa062b2d466c61a16e72e4a3f66af5059685efbb03b62c779bd80121cec2"
AT = datetime.fromtimestamp(1767225600, UTC)


def test_checkout_session_carries_amount_reference_and_key(recorder):  # noqa: F811
    recorder.add(
        "POST",
        "https://api.stripe.com/v1/checkout/sessions",
        200,
        {"id": "cs_1", "url": "https://checkout.stripe.com/c/cs_1", "expires_at": 1767229200},
    )
    session = STRIPE.create_checkout(
        Account(), INVOICE, customer_id="cus_1", return_url="https://app.test/back", idempotency_key="checkout:1"
    )
    assert (session.reference, session.url) == ("cs_1", "https://checkout.stripe.com/c/cs_1")
    _, url, headers, _ = recorder.sent[0]
    assert headers["Authorization"] == "Bearer sk_test_key" and headers["Idempotency-Key"] == "checkout:1"
    assert headers["Stripe-Version"] == "2024-06-20"
    form = recorder.form()
    assert form["mode"] == "payment" and form["line_items[0][price_data][currency]"] == "brl"
    assert form["line_items[0][price_data][unit_amount]"] == "4990"
    assert form["payment_intent_data[setup_future_usage]"] == "off_session"
    assert form["payment_intent_data[metadata][invoice_reference]"] == f"{ORG}:{INV}" == form["client_reference_id"]


def test_off_session_charge_success_and_decline(recorder):  # noqa: F811
    recorder.add("POST", "https://api.stripe.com/v1/payment_intents", 200, {"id": "pi_ok", "status": "succeeded"})
    result = STRIPE.charge_saved_method(
        Account(), INVOICE, customer_id="cus_1", method_ref="pm_1", idempotency_key="collect:x:0"
    )
    assert (result.reference, result.status) == ("pi_ok", PaymentState.SUCCEEDED)
    assert recorder.form()["off_session"] == "true" and recorder.form()["confirm"] == "true"
    recorder.add(
        "POST",
        "https://api.stripe.com/v1/payment_intents",
        402,
        {
            "error": {
                "code": "card_declined",
                "payment_intent": {"id": "pi_declined", "status": "requires_payment_method"},
            }
        },
    )
    declined = STRIPE.charge_saved_method(
        Account(), INVOICE, customer_id="cus_1", method_ref="pm_1", idempotency_key="collect:x:1"
    )
    assert (declined.reference, declined.status, declined.failure_code) == (
        "pi_declined",
        PaymentState.FAILED,
        "card_declined",
    )
    recorder.add(
        "POST",
        "https://api.stripe.com/v1/payment_intents",
        402,
        {"error": {"code": "authentication_required", "payment_intent": {"id": "pi_3ds", "status": "requires_action"}}},
    )
    assert (
        STRIPE.charge_saved_method(Account(), INVOICE, customer_id="c", method_ref="pm", idempotency_key="k").status
        == PaymentState.FAILED
    )


def test_fetch_resolves_a_checkout_session_and_reads_the_card(recorder):  # noqa: F811
    recorder.add(
        "GET", "https://api.stripe.com/v1/checkout/sessions/cs_1", 200, {"id": "cs_1", "payment_intent": "pi_1"}
    )
    recorder.add(
        "GET",
        "https://api.stripe.com/v1/payment_intents/pi_1",
        200,
        {
            "id": "pi_1",
            "status": "succeeded",
            "amount": 4990,
            "amount_received": 4990,
            "currency": "brl",
            "payment_method": "pm_1",
            "customer": "cus_1",
            "metadata": {"invoice_reference": f"{ORG}:{INV}"},
        },
    )
    recorder.add(
        "GET", "https://api.stripe.com/v1/payment_methods/pm_1", 200, {"card": {"brand": "visa", "last4": "4242"}}
    )
    status = STRIPE.fetch_payment(Account(), "cs_1")
    assert (status.reference, status.status, status.amount, status.currency) == (
        "pi_1",
        PaymentState.SUCCEEDED,
        4990,
        "BRL",
    )
    assert (status.invoice_reference, status.method_ref, status.method_label) == (
        f"{ORG}:{INV}",
        "pm_1",
        "Visa •••• 4242",
    )
    recorder.add(
        "GET",
        "https://api.stripe.com/v1/checkout/sessions/cs_2",
        200,
        {"id": "cs_2", "payment_intent": None, "amount_total": 4990, "currency": "brl", "client_reference_id": "r"},
    )
    assert STRIPE.fetch_payment(Account(), "cs_2").status == PaymentState.PENDING


def test_webhook_signature_vector_tolerance_and_rotation():
    account = Account()
    header = {"stripe-signature": f"t=1767225600,v1={VECTOR}"}
    note = STRIPE.verify_webhook(account, header, BODY, now=AT)
    assert (note.event_id, note.payment_reference, note.kind) == ("evt_1", "pi_1", "payment_intent.succeeded")
    rotated = {"stripe-signature": f"t=1767225600,v1={'0' * 64},v1={VECTOR}"}
    assert STRIPE.verify_webhook(account, rotated, BODY, now=AT).event_id == "evt_1"
    refused = [
        ({"stripe-signature": f"t=1767225600,v1={'0' * 64}"}, AT, "invalid_signature"),
        (header, datetime.fromtimestamp(1767225600 + 301, UTC), "stale_timestamp"),
        ({}, AT, "missing_signature"),
    ]
    for headers, now, code in refused:
        with pytest.raises(WebhookRejected) as error:
            STRIPE.verify_webhook(account, headers, BODY, now=now)
        assert error.value.code == code
    with pytest.raises(WebhookRejected):
        STRIPE.verify_webhook(account, header, BODY + b" ", now=AT)  # One changed byte.


def test_checkout_session_completed_names_the_payment_intent():
    import hashlib
    import hmac

    body = b'{"id":"evt_2","type":"checkout.session.completed","data":{"object":{"id":"cs_9","payment_intent":"pi_9"}}}'
    signature = hmac.new(b"whsec_test_vector", b"1767225600." + body, hashlib.sha256).hexdigest()
    note = STRIPE.verify_webhook(Account(), {"stripe-signature": f"t=1767225600,v1={signature}"}, body, now=AT)
    assert note.payment_reference == "pi_9"


def test_refund(recorder):  # noqa: F811
    recorder.add("POST", "https://api.stripe.com/v1/refunds", 200, {"id": "re_1", "status": "succeeded"})
    assert STRIPE.refund(Account(), "pi_1", amount=100, idempotency_key="refund:1").status == PaymentState.REFUNDED
    assert recorder.form() == {"payment_intent": "pi_1", "amount": "100"}
