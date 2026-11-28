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


def test_reconcile_uses_billing_account_provider_account_over_default(billing):
    """#886: reconcile queries the billing account's own ProviderAccount when multiple exist."""
    from skeleton.billing.models import BillingAccount, Payment, ProviderAccount

    scenario, provider, default_account, invoice = billing
    with system_context(reason="test:providers-second-account"):
        live_account = ProviderAccount(
            provider="fake",
            label="Fake Live",
            mode=ProviderAccount.Mode.LIVE,
            currencies=["BRL"],
            is_default=False,
        )
        live_account.set_secrets(api_key="sk_live_fake", webhook_secret="whsec_live_fake")
        live_account.save()

    seen_accounts = []
    original_fetch = provider.fetch_payment

    def tracking_fetch(acc, ref):
        seen_accounts.append(acc.pk)
        return original_fetch(acc, ref)

    provider.fetch_payment = tracking_fetch
    with in_a(scenario):
        BillingAccount.objects.update(provider_account=live_account)
        attempt = payments.create_checkout(invoice, return_url="https://app.example.test/back")
        status = original_fetch(live_account, attempt.reference)
        payments.apply_status(live_account, status)
        assert Payment.objects.get().status == "pending"
    provider.pay(attempt.reference)
    with in_a(scenario):
        assert payments.reconcile(now=datetime.now(UTC) + timedelta(hours=1)) == 1
    assert seen_accounts == [live_account.pk]
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


def test_refund_partial_full_and_duplicate_payment_update_access_events_and_alerts(billing):
    """#885: full refund/chargeback suspends access; partial & duplicate emit events and platform alerts."""
    from skeleton.billing.models import BillingAccount, Invoice, InvoiceLine, Payment
    from skeleton.events.models import DomainEvent
    from skeleton.notifications.models import PlatformNotification
    from skeleton.notifications.platform_alerts import BILLING_PAYMENT_DUPLICATED, BILLING_PAYMENT_REFUNDED

    scenario, provider, account, invoice = billing
    attempt1 = checkout(scenario, invoice)
    provider.pay(attempt1.reference)
    body, sig = FakeProvider.webhook(attempt1.reference, event_id="evt_pay_1")
    assert post(account, body, sig).status_code == 200
    run_pending_jobs()
    assert invoice_status(scenario, invoice) == "paid"
    with in_a(scenario):
        lines_before = list(InvoiceLine.objects.filter(invoice=invoice).values("sysid", "amount", "kind", "revision"))

    # Duplicate payment on already-paid invoice emits event + platform alert without changing invoice.
    from skeleton.billing.providers.base import PaymentStatus

    dup_ref = "pay_duplicate_1"

    provider.payments[dup_ref] = PaymentStatus(
        reference=dup_ref,
        status=PaymentState.SUCCEEDED,
        amount=invoice.total,
        currency=invoice.currency,
        invoice_reference=f"{scenario.a.pk}:{invoice.pk}",
    )
    body_dup, sig_dup = FakeProvider.webhook(dup_ref, event_id="evt_dup_1")
    assert post(account, body_dup, sig_dup).status_code == 200
    run_pending_jobs()
    assert invoice_status(scenario, invoice) == "paid"
    with in_a(scenario):
        assert DomainEvent.objects.filter(event_type="billing.payment_duplicated").count() == 1
        assert Payment.objects.filter(invoice=invoice, status="succeeded").count() == 2
    with system_context(reason="test:providers-alert-dup"):
        assert PlatformNotification.objects.filter(type_code=BILLING_PAYMENT_DUPLICATED).count() == 1

    # Partial refund keeps invoice paid and access active, while alerting the platform.
    provider.mark_refunded(attempt1.reference, amount=1000)
    body_part, sig_part = FakeProvider.webhook(attempt1.reference, event_id="evt_ref_part")
    assert post(account, body_part, sig_part).status_code == 200
    run_pending_jobs()
    with in_a(scenario):
        assert Invoice.objects.get(pk=invoice.pk).status == "paid"
        assert BillingAccount.objects.get().access_state == "active"
    with system_context(reason="test:providers-alert-part"):
        part_alert = PlatformNotification.objects.get(type_code=BILLING_PAYMENT_REFUNDED)
        assert part_alert.payload["partial"] is True

    # Full refund transitions invoice to refunded, suspends access, and keeps InvoiceLines unchanged.
    provider.mark_refunded(attempt1.reference)
    body_full, sig_full = FakeProvider.webhook(attempt1.reference, event_id="evt_ref_full")
    assert post(account, body_full, sig_full).status_code == 200
    run_pending_jobs()
    with in_a(scenario):
        refreshed = Invoice.objects.get(pk=invoice.pk)
        assert refreshed.status == "refunded" and refreshed.amount_paid == 0
        assert BillingAccount.objects.get().access_state == "suspended"
        assert DomainEvent.objects.filter(event_type="billing.payment_refunded").count() == 2
        assert list(InvoiceLine.objects.filter(invoice=invoice).values("sysid", "amount", "kind", "revision")) == (
            lines_before
        )
    with system_context(reason="test:providers-alert-full"):
        alerts = list(PlatformNotification.objects.filter(type_code=BILLING_PAYMENT_REFUNDED).order_by("created_at"))
        assert len(alerts) == 2 and alerts[-1].payload["partial"] is False
