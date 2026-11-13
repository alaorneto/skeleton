"""Mercado Pago adapter (#402) against recorded responses and a fixed signature vector. No network."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from skeleton.billing.providers.base import Capability, InvoiceData, PaymentState, ProviderError, WebhookRejected
from skeleton.billing.providers.mercadopago import PROVIDER as MP
from tests.unit.billing_recorded import Account, recorder  # noqa: F401

ORG = UUID("11111111-1111-1111-1111-111111111111")
INV = UUID("22222222-2222-2222-2222-222222222222")
INVOICE = InvoiceData(ORG, INV, "2026-000007", "BRL", 4990, "Fatura 2026-000007", datetime(2026, 3, 1, tzinfo=UTC))
VECTOR = "dfac2a0a85dd0520e49d3cf7682abb9a4a06760bd01758da044f5d022772578f"
AT = datetime.fromtimestamp(1767225600, UTC)


def account():
    return Account(api_key="APP_USR-token", webhook_secret="mp_secret_vector")


def test_checkout_pro_preference_in_decimals_with_the_reference(recorder):  # noqa: F811
    recorder.add(
        "POST",
        "https://api.mercadopago.com/checkout/preferences",
        201,
        {"id": "pref_1", "init_point": "https://mp.test/live", "sandbox_init_point": "https://mp.test/sandbox"},
    )
    session = MP.create_checkout(
        account(), INVOICE, customer_id="", return_url="https://app.test/b", idempotency_key="checkout:1"
    )
    assert (session.reference, session.url) == ("pref_1", "https://mp.test/sandbox")
    headers = recorder.sent[0][2]
    assert headers["Authorization"] == "Bearer APP_USR-token" and headers["X-Idempotency-Key"] == "checkout:1"
    body = recorder.json()
    assert body["items"][0]["unit_price"] == 49.9 and body["items"][0]["currency_id"] == "BRL"
    assert body["external_reference"] == f"{ORG}:{INV}"
    assert Capability.OFF_SESSION_CARD not in MP.capabilities(account())
    with pytest.raises(ProviderError):
        MP.charge_saved_method(account(), INVOICE, customer_id="c", method_ref="m", idempotency_key="k")


def test_pix_pending_then_approved_and_a_rejected_boleto(recorder, subtests):  # noqa: F811
    cases = [
        ({"status": "pending", "payment_type_id": "bank_transfer"}, PaymentState.PENDING, "pix"),
        ({"status": "approved", "payment_type_id": "bank_transfer"}, PaymentState.SUCCEEDED, "pix"),
        ({"status": "cancelled", "payment_type_id": "ticket"}, PaymentState.CANCELED, "boleto"),
        (
            {
                "status": "rejected",
                "payment_type_id": "credit_card",
                "status_detail": "cc_rejected_insufficient_amount",
            },
            PaymentState.FAILED,
            "card",
        ),
        ({"status": "refunded", "payment_type_id": "credit_card"}, PaymentState.REFUNDED, "card"),
    ]
    for fields, state, method in cases:
        recorder.add(
            "GET",
            "https://api.mercadopago.com/v1/payments/123456",
            200,
            {
                "id": 123456,
                "transaction_amount": 49.9,
                "currency_id": "BRL",
                "external_reference": f"{ORG}:{INV}",
                **fields,
            },
        )
        with subtests.test(status=fields["status"]):
            status = MP.fetch_payment(account(), "123456")
            assert (status.status, status.method, status.amount, status.reference) == (state, method, 4990, "123456")
            assert status.invoice_reference == f"{ORG}:{INV}"


def test_notification_signature_vector_and_refusals():
    headers = {"x-signature": f"ts=1767225600,v1={VECTOR}", "x-request-id": "req-1"}
    body = b'{"id": 999, "type": "payment", "data": {"id": "123456"}}'
    note = MP.verify_webhook(account(), headers, body, now=AT, query={"data.id": "123456", "type": "payment"})
    assert (note.event_id, note.payment_reference, note.kind) == ("999", "123456", "payment")
    refusals = [
        ({**headers, "x-request-id": "req-2"}, AT, "invalid_signature"),
        ({"x-signature": f"ts=1767225600,v1={'0' * 64}", "x-request-id": "req-1"}, AT, "invalid_signature"),
        (headers, datetime.fromtimestamp(1767225600 + 400, UTC), "stale_timestamp"),
        ({}, AT, "missing_signature"),
    ]
    for sent, now, code in refusals:
        with pytest.raises(WebhookRejected) as error:
            MP.verify_webhook(account(), sent, body, now=now, query={"data.id": "123456"})
        assert error.value.code == code


def test_customer_found_by_email_or_created(recorder):  # noqa: F811
    from skeleton.billing.providers.base import CustomerData

    customer = CustomerData(ORG, "Empresa", "fin@empresa.test", "12.345.678/0001-99", "BR")
    recorder.add("GET", "https://api.mercadopago.com/v1/customers/search", 200, {"results": [{"id": "cust_old"}]})
    assert MP.ensure_customer(account(), customer) == "cust_old"
    recorder.add("GET", "https://api.mercadopago.com/v1/customers/search", 200, {"results": []})
    recorder.add("POST", "https://api.mercadopago.com/v1/customers", 201, {"id": "cust_new"})
    assert MP.ensure_customer(account(), customer) == "cust_new"
    assert recorder.json()["identification"] == {"type": "CNPJ", "number": "12345678000199"}
