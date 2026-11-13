"""Stripe end to end (#401): checkout, a signed webhook and the fetched PaymentIntent pay the invoice.

Runs on PostgreSQL with the runtime role; the Stripe API answers are recorded, no network.
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
SECRET = "whsec_stripe_flow"  # noqa: S105 - fixed secret of a local test


def test_stripe_checkout_webhook_and_fetch_pay_the_invoice(skeleton_db, settings, recorder):  # noqa: F811
    from skeleton.billing.models import Invoice, Price, Product, ProviderAccount

    settings.BILLING_ENCRYPTION_KEYS = [Fernet.generate_key().decode()]
    scenario = build_service_scenario(Note, slug="billing-stripe")
    settings.SKELETON_BILLING_ENABLED = True
    with system_context(reason="test:stripe-catalog"):
        plan = Product(code="plan", kind="base_plan", name="Plano")
        plan.save()
        price = Price(product=plan, currency="BRL", unit_amount=4990)
        price.save()
        account = ProviderAccount(provider="stripe", label="Stripe teste", is_default=True)
        account.set_secrets(api_key="sk_test_flow", webhook_secret=SECRET)
        account.save()
    with system_context(reason="test:stripe-start", organization_id=scenario.a.pk):
        engine.start_subscription(price, now=MAR1)
        invoice = Invoice.objects.get()
        reference = f"{scenario.a.pk}:{invoice.pk}"
        recorder.add("POST", "https://api.stripe.com/v1/customers", 200, {"id": "cus_flow"})
        recorder.add(
            "POST",
            "https://api.stripe.com/v1/checkout/sessions",
            200,
            {"id": "cs_flow", "url": "https://checkout.stripe.com/c/cs_flow"},
        )
        attempt = payments.create_checkout(invoice, return_url="https://app.example.test/back")
    assert attempt.url == "https://checkout.stripe.com/c/cs_flow"
    body = json.dumps(
        {
            "id": "evt_flow",
            "type": "checkout.session.completed",
            "data": {"object": {"id": "cs_flow", "payment_intent": "pi_flow"}},
        }
    ).encode()
    stamp = int(time.time())
    signature = hmac.new(SECRET.encode(), f"{stamp}.".encode() + body, hashlib.sha256).hexdigest()
    url = reverse("billing_webhooks:receive", kwargs={"provider": "stripe", "account_id": account.pk})
    response = Client().post(
        url, body, content_type="application/json", HTTP_STRIPE_SIGNATURE=f"t={stamp},v1={signature}"
    )
    assert response.status_code == 200
    recorder.add(
        "GET",
        "https://api.stripe.com/v1/payment_intents/pi_flow",
        200,
        {
            "id": "pi_flow",
            "status": "succeeded",
            "amount_received": 4990,
            "currency": "brl",
            "payment_method": "pm_flow",
            "customer": "cus_flow",
            "metadata": {"invoice_reference": reference},
        },
    )
    recorder.add(
        "GET",
        "https://api.stripe.com/v1/payment_methods/pm_flow",
        200,
        {"card": {"brand": "mastercard", "last4": "4444"}},
    )
    run_pending_jobs()
    with system_context(reason="test:stripe-read", organization_id=scenario.a.pk):
        paid = Invoice.objects.get(pk=invoice.pk)
        assert paid.status == "paid"
        assert (
            paid.account.payment_method_label == "Mastercard •••• 4444" and paid.account.payment_method_ref == "pm_flow"
        )
    stale = Client().post(
        url, body, content_type="application/json", HTTP_STRIPE_SIGNATURE=f"t={stamp - 600},v1={signature}"
    )
    assert stale.status_code == 400
