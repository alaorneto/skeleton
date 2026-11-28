"""Two workers renewing the same period produce one invoice (#396)."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier

import pytest
from django.db import connection, connections

from tools.integration_database import disposable_database_pair

MAR1 = datetime(2026, 3, 1, tzinfo=UTC)
APR1 = datetime(2026, 4, 1, tzinfo=UTC)


@pytest.mark.needs_example_app
def test_concurrent_renewals_issue_one_invoice(skeleton_db, settings):
    from examples.notes.models import Note
    from skeleton.billing import engine
    from skeleton.billing.models import Invoice, Price, Product
    from skeleton.tenancy.context import system_context
    from skeleton.testing.service_api import build_service_scenario

    disposable_database_pair()
    assert connection.vendor == "postgresql"
    scenario = build_service_scenario(Note, slug="billing-renewal")
    settings.SKELETON_BILLING_ENABLED = True
    with system_context(reason="test:billing-renewal-catalog"):
        product = Product(code="plan", kind="base_plan", name="Plano")
        product.save()
        price = Price(product=product, currency="BRL", unit_amount=5000)
        price.save()
    with system_context(reason="test:billing-renewal-start", organization_id=scenario.a.pk):
        engine.start_subscription(price, now=MAR1)
        engine.settle_invoice(Invoice.objects.get(), now=MAR1)
    workers = 4
    barrier = Barrier(workers)

    def renew(_):
        connections.close_all()
        try:
            with system_context(reason="test:billing-renewal", organization_id=scenario.a.pk):
                barrier.wait(10)
                invoice = engine.renew_due(now=APR1)
                return invoice.number if invoice else None
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(renew, range(workers), timeout=60))
    assert results.count(None) == workers - 1 and "2026-000002" in results
    with system_context(reason="test:billing-renewal-read", organization_id=scenario.a.pk):
        assert Invoice.objects.filter(period_start=APR1).count() == 1


@pytest.mark.needs_example_app
def test_concurrent_plan_changes_allow_at_most_one_pending_upgrade(skeleton_db, settings):
    """#883: two concurrent upgrades serialize on the subscription and only one succeeds."""
    from datetime import timedelta

    from examples.notes.models import Note
    from skeleton.billing import engine
    from skeleton.billing.models import Invoice, Price, Product, SubscriptionItem
    from skeleton.tenancy.context import system_context
    from skeleton.testing.service_api import build_service_scenario

    disposable_database_pair()
    assert connection.vendor == "postgresql"
    scenario = build_service_scenario(Note, slug="billing-upgrade-race")
    settings.SKELETON_BILLING_ENABLED = True
    with system_context(reason="test:billing-upgrade-catalog"):
        basic = Product(code="basic-race", kind="base_plan", name="Básico")
        pro = Product(code="pro-race", kind="base_plan", name="Pro")
        ent = Product(code="ent-race", kind="base_plan", name="Enterprise")
        for product in (basic, pro, ent):
            product.save()
        basic_p = Price(product=basic, currency="BRL", unit_amount=10000)
        pro_p = Price(product=pro, currency="BRL", unit_amount=20000)
        ent_p = Price(product=ent, currency="BRL", unit_amount=30000)
        for price in (basic_p, pro_p, ent_p):
            price.save()
    with system_context(reason="test:billing-upgrade-start", organization_id=scenario.a.pk):
        engine.start_subscription(basic_p, now=MAR1)
        engine.settle_invoice(Invoice.objects.get(), now=MAR1)

    barrier = Barrier(2)
    targets = [pro_p, ent_p]

    def upgrade(price):
        connections.close_all()
        try:
            with system_context(reason="test:billing-upgrade", organization_id=scenario.a.pk):
                barrier.wait(10)
                try:
                    engine.change_plan(price, now=MAR1 + timedelta(days=10))
                    return "ok"
                except engine.PlanChangePending:
                    return "pending"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(upgrade, targets, timeout=60))
    assert sorted(results) == ["ok", "pending"]
    with system_context(reason="test:billing-upgrade-verify", organization_id=scenario.a.pk):
        base_items = SubscriptionItem.objects.filter(price__product__kind="base_plan")
        assert base_items.filter(status="active").count() == 1
        assert base_items.filter(status="pending").count() == 1
        open_inv = Invoice.objects.get(status="open")
        engine.settle_invoice(open_inv, now=MAR1 + timedelta(days=10))
        assert base_items.filter(status="active").count() == 1
        assert base_items.filter(status="pending").count() == 0


@pytest.mark.needs_example_app
def test_concurrent_duplicate_webhooks_return_200_and_record_one_event(skeleton_db, settings):
    """#886: two concurrent deliveries of the same webhook both return 200 and record one ProviderEvent."""
    from cryptography.fernet import Fernet
    from django.test import Client
    from django.urls import reverse

    from skeleton.billing.models import ProviderEvent
    from skeleton.tenancy.context import system_context
    from skeleton.testing.billing import FakeProvider, use_fake_provider

    disposable_database_pair()
    assert connection.vendor == "postgresql"
    settings.BILLING_ENCRYPTION_KEYS = [Fernet.generate_key().decode()]
    settings.SKELETON_BILLING_ENABLED = True
    with use_fake_provider() as (_provider, account):
        url = reverse("billing_webhooks:receive", kwargs={"provider": "fake", "account_id": account.pk})
        body, signature = FakeProvider.webhook("pay_race_1", event_id="evt_race_1")
        barrier = Barrier(2)

        def deliver(_):
            connections.close_all()
            try:
                client = Client()
                barrier.wait(10)
                response = client.post(url, body, content_type="application/json", HTTP_X_FAKE_SIGNATURE=signature)
                return response.status_code
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(deliver, range(2), timeout=60))
        assert statuses == [200, 200]
        with system_context(reason="test:webhook-race-read"):
            assert ProviderEvent.objects.filter(provider_account=account, event_id="evt_race_1").count() == 1
