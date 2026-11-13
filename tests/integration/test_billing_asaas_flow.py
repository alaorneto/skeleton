"""Asaas end to end (#403): the token webhook, the fetched charge and the BRL-only account.

Runs on PostgreSQL with the runtime role; the Asaas answers are recorded, no network.
"""

import json
from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse

from examples.notes.models import Note
from skeleton.billing import engine, payments
from skeleton.billing.providers.asaas import SANDBOX_URL
from skeleton.tenancy.context import system_context
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.service_api import build_service_scenario
from tests.unit.billing_recorded import recorder  # noqa: F401

pytestmark = pytest.mark.needs_example_app
MAR1 = datetime(2026, 3, 1, tzinfo=UTC)
TOKEN = "asaas-flow-token-abc"  # noqa: S105 - fixed token of a local test


def test_asaas_charge_is_paid_by_a_token_webhook_after_the_fetch(skeleton_db, settings, recorder):  # noqa: F811
    from skeleton.billing.models import BillingAccount, Invoice, Price, Product, ProviderAccount

    settings.BILLING_ENCRYPTION_KEYS = [Fernet.generate_key().decode()]
    scenario = build_service_scenario(Note, slug="billing-asaas")
    settings.SKELETON_BILLING_ENABLED = True
    with system_context(reason="test:asaas-catalog"):
        with pytest.raises(ValidationError):
            ProviderAccount(provider="asaas", label="USD", currencies=["USD"]).save()
        plan = Product(code="plan", kind="base_plan", name="Plano")
        plan.save()
        price = Price(product=plan, currency="BRL", unit_amount=4990)
        price.save()
        account = ProviderAccount(provider="asaas", label="Asaas teste", is_default=True)
        account.set_secrets(api_key="$aact_flow", webhook_secret=TOKEN)
        account.save()
        assert account.currencies == ["BRL"]
    with system_context(reason="test:asaas-start", organization_id=scenario.a.pk):
        engine.start_subscription(price, now=MAR1)
        BillingAccount.objects.update(legal_name="Empresa", tax_id="12.345.678/0001-99")
        invoice = Invoice.objects.get()
        reference = f"{scenario.a.pk}:{invoice.pk}"
        recorder.add("GET", f"{SANDBOX_URL}/customers?externalReference=", 200, {"data": []})
        recorder.add("POST", f"{SANDBOX_URL}/customers", 200, {"id": "cus_flow"})
        recorder.add("GET", f"{SANDBOX_URL}/payments?externalReference=", 200, {"data": []})
        recorder.add(
            "POST", f"{SANDBOX_URL}/payments", 200, {"id": "pay_flow", "invoiceUrl": "https://asaas.test/i/pay_flow"}
        )
        attempt = payments.create_checkout(invoice, return_url="https://app.example.test/back")
    assert attempt.url == "https://asaas.test/i/pay_flow"
    url = reverse("billing_webhooks:receive", kwargs={"provider": "asaas", "account_id": account.pk})
    body = json.dumps(
        {"id": "evt_flow", "event": "PAYMENT_RECEIVED", "payment": {"id": "pay_flow", "status": "RECEIVED"}}
    )
    assert Client().post(url, body, content_type="application/json", HTTP_ASAAS_ACCESS_TOKEN="wrong").status_code == 400
    assert Client().post(url, body, content_type="application/json", HTTP_ASAAS_ACCESS_TOKEN=TOKEN).status_code == 200
    recorder.add(
        "GET",
        f"{SANDBOX_URL}/payments/pay_flow",
        200,
        {"id": "pay_flow", "status": "RECEIVED", "value": 49.9, "billingType": "PIX", "externalReference": reference},
    )
    run_pending_jobs()
    with system_context(reason="test:asaas-read", organization_id=scenario.a.pk):
        assert Invoice.objects.get(pk=invoice.pk).status == "paid"
