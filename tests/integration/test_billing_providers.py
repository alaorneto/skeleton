"""Payment provider interface (#400): checkout, inbound webhooks, reconciliation and auto charge.

Runs on PostgreSQL with the runtime role, with the in-memory FakeProvider and no network.
"""

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from django.test import Client
from django.urls import reverse

from examples.notes.models import Note
from skeleton.billing import engine, payments
from skeleton.billing.providers.base import PaymentState
from skeleton.tenancy.context import system_context
from skeleton.testing.billing import FakeProvider, use_fake_provider
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.service_api import build_service_scenario

pytestmark = pytest.mark.needs_example_app
MAR1 = datetime(2026, 3, 1, tzinfo=UTC)


@pytest.fixture
def billing(skeleton_db, settings):
    from skeleton.billing.models import Price, Product

    settings.BILLING_ENCRYPTION_KEYS = [Fernet.generate_key().decode()]
    scenario = build_service_scenario(Note, slug="billing-providers")
    settings.SKELETON_BILLING_ENABLED = True
    with system_context(reason="test:providers-catalog"):
        plan = Product(code="plan", kind="base_plan", name="Plano")
        plan.save()
        price = Price(product=plan, currency="BRL", unit_amount=4990)
        price.save()
    with use_fake_provider() as (provider, account):
        with system_context(reason="test:providers-start", organization_id=scenario.a.pk):
            engine.start_subscription(price, now=MAR1)
            invoice = engine.current_subscription().invoices.get()
        yield scenario, provider, account, invoice


def in_a(scenario):
    return system_context(reason="test:providers", organization_id=scenario.a.pk)


def webhook_url(account):
    return reverse("billing_webhooks:receive", kwargs={"provider": "fake", "account_id": account.pk})


def post(account, body, signature):
    return Client().post(webhook_url(account), body, content_type="application/json", HTTP_X_FAKE_SIGNATURE=signature)


def checkout(scenario, invoice):
    with in_a(scenario):
        return payments.create_checkout(invoice, return_url="https://app.example.test/back")


def invoice_status(scenario, invoice):
    from skeleton.billing.models import Invoice

    with in_a(scenario):
        return Invoice.objects.get(pk=invoice.pk).status


def test_checkout_then_a_verified_webhook_pays_after_fetching_the_payment(billing):
    from skeleton.billing.models import BillingAccount, Payment, ProviderEvent

    scenario, provider, account, invoice = billing
    attempt = checkout(scenario, invoice)
    assert attempt.status == "open" and attempt.url.startswith("https://fake.example.test/pay/")
    assert ("create_checkout", f"checkout:{attempt.pk}") in provider.calls
    provider.pay(attempt.reference)
    body, signature = FakeProvider.webhook(attempt.reference, event_id="evt_1")
    assert post(account, body, signature).status_code == 200
    run_pending_jobs()
    assert invoice_status(scenario, invoice) == "paid"
    assert ("fetch", attempt.reference) in provider.calls
    with in_a(scenario):
        payment = Payment.objects.get()
        assert (payment.status, payment.amount, payment.method) == ("succeeded", 4990, "card")
        billing_account = BillingAccount.objects.get()
        assert billing_account.payment_method_label == "Visa •••• 4242"
    # The same event again is acknowledged and changes nothing.
    duplicate = post(account, body, signature)
    assert duplicate.status_code == 200 and duplicate.json()["duplicate"] is True
    with system_context(reason="test:providers-events"):
        assert ProviderEvent.objects.filter(event_id="evt_1").count() == 1


def test_bad_signature_and_forged_bodies_have_no_effect(billing):
    from skeleton.billing.models import ProviderEvent

    scenario, provider, account, invoice = billing
    attempt = checkout(scenario, invoice)
    body, signature = FakeProvider.webhook(attempt.reference)
    assert post(account, body, "0" * 64).status_code == 400
    flipped = signature[:-1] + ("1" if signature[-1] == "0" else "0")  # One character changed.
    assert post(account, body, flipped).status_code == 400
    # Authentic notification, but the provider says the payment is still pending: nothing is paid.
    assert post(account, body, signature).status_code == 200
    run_pending_jobs()
    assert invoice_status(scenario, invoice) == "open"
    with system_context(reason="test:providers-events"):
        assert ProviderEvent.objects.get().outcome == "unchanged"  # Recorded as pending, nothing paid.
    other = Client().post(
        reverse("billing_webhooks:receive", kwargs={"provider": "fake", "account_id": invoice.pk}),
        body,
        content_type="application/json",
        HTTP_X_FAKE_SIGNATURE=signature,
    )
    assert other.status_code == 404
    too_big = Client().post(
        webhook_url(account), b"x" * (300 * 1024), content_type="application/json", HTTP_X_FAKE_SIGNATURE=signature
    )
    assert too_big.status_code == 413


def test_a_wrong_amount_or_currency_never_marks_the_invoice_paid(billing):
    from skeleton.billing.models import ProviderEvent

    scenario, provider, account, invoice = billing
    attempt = checkout(scenario, invoice)
    provider.pay(attempt.reference, amount=100)
    body, signature = FakeProvider.webhook(attempt.reference)
    post(account, body, signature)
    run_pending_jobs()
    assert invoice_status(scenario, invoice) == "open"
    with system_context(reason="test:providers-events"):
        event = ProviderEvent.objects.get()
        assert (event.status, event.outcome) == ("failed", "amount_mismatch")


def test_a_lost_webhook_is_reconciled(billing):
    from skeleton.billing.models import Payment

    scenario, provider, account, invoice = billing
    attempt = checkout(scenario, invoice)
    with in_a(scenario):
        status = provider.fetch_payment(account, attempt.reference)
        payments.apply_status(account, status)  # A pending payment is recorded.
        assert Payment.objects.get().status == "pending"
    provider.pay(attempt.reference)
    with in_a(scenario):
        assert payments.reconcile() == 0  # Too recent: a webhook may still come.
        assert payments.reconcile(now=datetime.now(UTC) + timedelta(hours=1)) == 1
    assert invoice_status(scenario, invoice) == "paid"


def test_auto_charge_is_idempotent_and_a_decline_schedules_the_retry(billing):
    from skeleton.billing.models import BillingAccount, Invoice, Subscription

    scenario, provider, account, invoice = billing
    with in_a(scenario):
        Subscription.objects.update(collection="auto_charge")
        BillingAccount.objects.update(
            payment_method_ref="pm_saved", provider_customer_id="cus_1", provider_account=account
        )
        provider.next_charge = PaymentState.FAILED
        assert payments.collect(invoice) == "failed"
        assert payments.collect(invoice) == "unchanged"  # Same attempt, same key: one charge at the provider.
        refreshed = Invoice.objects.get(pk=invoice.pk)
        assert refreshed.status == "open" and refreshed.next_attempt_at == invoice.due_at + timedelta(days=1)
    charges = [key for name, key in provider.calls if name == "charge"]
    assert charges == [f"collect:{invoice.pk}:0", f"collect:{invoice.pk}:0"]
    with in_a(scenario):
        provider.next_charge = PaymentState.SUCCEEDED
        Invoice.objects.filter(pk=invoice.pk).update(attempt_count=1)
        assert payments.collect(Invoice.objects.get(pk=invoice.pk)) == "paid"


def test_checkout_without_an_enabled_provider_is_refused(billing):
    from django.core.exceptions import ValidationError

    from skeleton.billing.models import ProviderAccount

    scenario, provider, account, invoice = billing
    with system_context(reason="test:providers-disable"):
        ProviderAccount.objects.filter(pk=account.pk).update(enabled=False)
    with pytest.raises(ValidationError):
        checkout(scenario, invoice)


def test_provider_secrets_are_encrypted_and_redacted(billing):
    from django.db import connection

    from skeleton.audit.models import AuditEvent

    scenario, provider, account, invoice = billing
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT api_key, webhook_secret FROM skeleton.sk_billing_provideraccount WHERE sysid = %s", [account.pk]
        )
        api_key, secret = cursor.fetchone()
    assert "sk_fake" not in api_key and "whsec" not in secret
    from uuid import uuid4

    from skeleton.accounts.services import bootstrap_identity
    from skeleton.tenancy.context import request_context

    platform, _ = bootstrap_identity(
        email="billing-platform@example.test",
        organization="billing-platform",
        name="Platform",
        password=scenario.password,
    )
    with request_context(actor=platform, request_id=uuid4(), source="test:providers-audit"):
        [event] = AuditEvent.objects.filter(record_sysid=account.pk)
    assert event.changes["api_key"]["redacted"] is True and event.changes["webhook_secret"]["redacted"] is True
