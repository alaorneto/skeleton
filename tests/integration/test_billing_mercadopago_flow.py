"""Mercado Pago end to end (#402): a Pix pending then approved, notified with a signed query.

Runs on PostgreSQL with the runtime role; the Mercado Pago answers are recorded, no network.
"""

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet
from django.test import Client
from django.urls import reverse

from examples.notes.models import Note
from skeleton.billing import engine, payments
from skeleton.tenancy.context import system_context
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.service_api import build_service_scenario
from tests.unit.billing_recorded import recorder  # noqa: F401

pytestmark = pytest.mark.needs_example_app
MAR1 = datetime(2026, 3, 1, tzinfo=UTC)
SECRET = "mp_flow_secret"  # noqa: S105 - fixed secret of a local test


def notify(account, payment_id, request_id):
    stamp = int(time.time())
    manifest = f"id:{payment_id};request-id:{request_id};ts:{stamp};"
    signature = hmac.new(SECRET.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    url = reverse("billing_webhooks:receive", kwargs={"provider": "mercadopago", "account_id": account.pk})
    body = json.dumps({"id": f"n-{request_id}", "type": "payment", "data": {"id": payment_id}}).encode()
    return Client().post(
        f"{url}?data.id={payment_id}&type=payment",
        body,
        content_type="application/json",
        HTTP_X_SIGNATURE=f"ts={stamp},v1={signature}",
        HTTP_X_REQUEST_ID=request_id,
    )


def test_pix_pending_then_approved_pays_the_invoice(skeleton_db, settings, recorder):  # noqa: F811
    from skeleton.billing.models import Invoice, Payment, Price, Product, ProviderAccount

    settings.BILLING_ENCRYPTION_KEYS = [Fernet.generate_key().decode()]
    scenario = build_service_scenario(Note, slug="billing-mp")
    settings.SKELETON_BILLING_ENABLED = True
    with system_context(reason="test:mp-catalog"):
        plan = Product(code="plan", kind="base_plan", name="Plano")
        plan.save()
        price = Price(product=plan, currency="BRL", unit_amount=4990)
        price.save()
        account = ProviderAccount(provider="mercadopago", label="MP teste", currencies=["BRL"], is_default=True)
        account.set_secrets(api_key="TEST-token", webhook_secret=SECRET)
        account.save()
    with system_context(reason="test:mp-start", organization_id=scenario.a.pk):
        engine.start_subscription(price, now=MAR1)
        invoice = Invoice.objects.get()
        recorder.add(
            "POST",
            "https://api.mercadopago.com/checkout/preferences",
            201,
            {"id": "pref_flow", "init_point": "https://mp.test/live", "sandbox_init_point": "https://mp.test/sandbox"},
        )
        attempt = payments.create_checkout(invoice, return_url="https://app.example.test/back")
    assert attempt.url == "https://mp.test/sandbox"
    payment = {
        "id": 555,
        "transaction_amount": 49.9,
        "currency_id": "BRL",
        "external_reference": f"{scenario.a.pk}:{invoice.pk}",
        "payment_type_id": "bank_transfer",
    }
    recorder.add("GET", "https://api.mercadopago.com/v1/payments/555", 200, {**payment, "status": "pending"})
    assert notify(account, "555", "req-a").status_code == 200
    run_pending_jobs()
    with system_context(reason="test:mp-read", organization_id=scenario.a.pk):
        assert Invoice.objects.get(pk=invoice.pk).status == "open"
        assert Payment.objects.get().method == "pix"
    recorder.add("GET", "https://api.mercadopago.com/v1/payments/555", 200, {**payment, "status": "approved"})
    assert notify(account, "555", "req-b").status_code == 200
    run_pending_jobs()
    with system_context(reason="test:mp-read-2", organization_id=scenario.a.pk):
        assert Invoice.objects.get(pk=invoice.pk).status == "paid"
        assert Payment.objects.get().status == "succeeded"
    forged = Client().post(
        reverse("billing_webhooks:receive", kwargs={"provider": "mercadopago", "account_id": account.pk})
        + "?data.id=555",
        b"{}",
        content_type="application/json",
        HTTP_X_SIGNATURE=f"ts={int(time.time())},v1={'0' * 64}",
        HTTP_X_REQUEST_ID="req-c",
    )
    assert forged.status_code == 400
