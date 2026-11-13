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
