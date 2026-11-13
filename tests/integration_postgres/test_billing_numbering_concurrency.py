"""Concurrent invoice numbering gets distinct, consecutive numbers without deadlock (#394)."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier

import pytest
from django.db import connection, connections

from tools.integration_database import disposable_database_pair

AT = datetime(2026, 5, 1, tzinfo=UTC)


@pytest.mark.parametrize("existing", [False, True], ids=["first-creation", "existing-counter"])
def test_concurrent_invoice_numbers_are_consecutive(skeleton_db, existing):
    from skeleton.billing.models import InvoiceCounter
    from skeleton.billing.numbering import next_invoice_number
    from skeleton.tenancy.context import system_context

    disposable_database_pair()
    assert connection.vendor == "postgresql"
    if existing:
        with system_context(reason="test:billing-number-seed"):
            next_invoice_number(at=AT)
    workers = 6
    barrier = Barrier(workers)

    def allocate(_):
        connections.close_all()
        try:
            with system_context(reason="test:billing-number"):
                barrier.wait(10)
                return next_invoice_number(at=AT)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        numbers = sorted(pool.map(allocate, range(workers), timeout=60))
    start = 2 if existing else 1
    assert numbers == [f"2026-{n:06d}" for n in range(start, start + workers)]
    with system_context(reason="test:billing-number-read"):
        assert InvoiceCounter.objects.get(year=2026).last_value == start + workers - 1
