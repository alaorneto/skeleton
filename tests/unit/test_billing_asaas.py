"""Asaas adapter (#403) against recorded responses. No network."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from skeleton.billing.providers.asaas import PROVIDER as ASAAS
from skeleton.billing.providers.asaas import SANDBOX_URL
from skeleton.billing.providers.base import CustomerData, InvoiceData, PaymentState, ProviderError, WebhookRejected
from tests.unit.billing_recorded import Account, recorder  # noqa: F401

ORG = UUID("11111111-1111-1111-1111-111111111111")
INV = UUID("22222222-2222-2222-2222-222222222222")
INVOICE = InvoiceData(ORG, INV, "2026-000007", "BRL", 4990, "Fatura 2026-000007", datetime(2026, 3, 1, tzinfo=UTC))
AT = datetime(2026, 3, 1, tzinfo=UTC)


def account():
    return Account(api_key="$aact_test", webhook_secret="asaas-webhook-token-123")


def test_checkout_reuses_an_open_charge_instead_of_creating_a_second(recorder):  # noqa: F811
    recorder.add("GET", f"{SANDBOX_URL}/payments?externalReference=", 200, {"data": []})
    recorder.add("POST", f"{SANDBOX_URL}/payments", 200, {"id": "pay_1", "invoiceUrl": "https://asaas.test/i/pay_1"})
    first = ASAAS.create_checkout(
        account(), INVOICE, customer_id="cus_1", return_url="https://app.test", idempotency_key="k1"
    )
    assert (first.reference, first.url) == ("pay_1", "https://asaas.test/i/pay_1")
    body = recorder.json()
    assert body == {
        "customer": "cus_1",
        "billingType": "UNDEFINED",
        "value": 49.9,
        "dueDate": "2026-03-01",
        "description": "Fatura 2026-000007",
        "externalReference": f"{ORG}:{INV}",
    }
    assert recorder.sent[1][2]["access_token"] == "$aact_test"
    recorder.add(
        "GET",
        f"{SANDBOX_URL}/payments?externalReference=",
        200,
        {"data": [{"id": "pay_1", "invoiceUrl": "https://asaas.test/i/pay_1"}]},
    )
    again = ASAAS.create_checkout(
        account(), INVOICE, customer_id="cus_1", return_url="https://app.test", idempotency_key="k2"
    )
    assert again.reference == "pay_1" and [method for method, *_ in recorder.sent].count("POST") == 1


def test_tokenized_card_charge_and_fetch(recorder, subtests):  # noqa: F811
    recorder.add("GET", f"{SANDBOX_URL}/payments?externalReference=", 200, {"data": []})
    recorder.add("POST", f"{SANDBOX_URL}/payments", 200, {"id": "pay_2", "status": "CONFIRMED"})
    result = ASAAS.charge_saved_method(account(), INVOICE, customer_id="cus_1", method_ref="tok_1", idempotency_key="k")
    assert (result.reference, result.status) == ("pay_2", PaymentState.SUCCEEDED)
    assert recorder.json()["creditCardToken"] == "tok_1" and recorder.json()["billingType"] == "CREDIT_CARD"
    for raw, state in (
        ("PENDING", PaymentState.PENDING),
        ("OVERDUE", PaymentState.PENDING),
        ("RECEIVED", PaymentState.SUCCEEDED),
        ("REFUNDED", PaymentState.REFUNDED),
    ):
        recorder.add(
            "GET",
            f"{SANDBOX_URL}/payments/pay_2",
            200,
            {
                "id": "pay_2",
                "status": raw,
                "value": 49.9,
                "billingType": "CREDIT_CARD",
                "externalReference": f"{ORG}:{INV}",
                "creditCard": {"creditCardToken": "tok_2", "creditCardBrand": "VISA", "creditCardNumber": "4242"},
            },
        )
        with subtests.test(status=raw):
            status = ASAAS.fetch_payment(account(), "pay_2")
            assert (status.status, status.amount, status.currency) == (state, 4990, "BRL")
            assert status.method_ref == ("tok_2" if state == PaymentState.SUCCEEDED else "")


def test_webhook_token_is_compared_and_the_body_names_the_payment():
    body = b'{"id": "evt_1", "event": "PAYMENT_RECEIVED", "payment": {"id": "pay_3"}}'
    note = ASAAS.verify_webhook(account(), {"asaas-access-token": "asaas-webhook-token-123"}, body, now=AT)
    assert (note.event_id, note.payment_reference, note.kind) == ("evt_1", "pay_3", "PAYMENT_RECEIVED")
    for headers in ({"asaas-access-token": "wrong"}, {}):
        with pytest.raises(WebhookRejected):
            ASAAS.verify_webhook(account(), headers, body, now=AT)


def test_brl_only_and_tax_id_required(recorder):  # noqa: F811
    usd = InvoiceData(ORG, INV, "2026-000008", "USD", 100, "x", AT)
    with pytest.raises(ProviderError):
        ASAAS.create_checkout(account(), usd, customer_id="c", return_url="r", idempotency_key="k")
    recorder.add("GET", f"{SANDBOX_URL}/customers?externalReference=", 200, {"data": []})
    with pytest.raises(ProviderError) as missing:
        ASAAS.ensure_customer(account(), CustomerData(ORG, "Empresa", "", "", "BR"))
    assert missing.value.code == "tax_id_required"
    assert ASAAS.currencies(account()) == frozenset({"BRL"})
