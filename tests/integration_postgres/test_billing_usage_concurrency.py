"""Two transactions racing for the last unit of an allowance: one records, the other is refused (#523, #525)."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from django.db import connection, connections

from tools.integration_database import disposable_database_pair

MAR1 = datetime(2026, 3, 1, tzinfo=UTC)
M = "example_notes.messages"


@pytest.mark.needs_example_app
@pytest.mark.parametrize("seeded", [False, True], ids=["first-counter", "existing-counter"])
def test_the_last_unit_has_one_winner(skeleton_db, settings, monkeypatch, seeded):
    from examples.notes.models import Note
    from skeleton.billing import usage
    from skeleton.billing.models import UsageBalance, UsageEvent
    from skeleton.common.exceptions import QuotaLimitExceeded
    from skeleton.tenancy.context import system_context
    from skeleton.testing.billing import subscribe_with_usage
    from skeleton.testing.service_api import build_service_scenario

    disposable_database_pair()
    assert connection.vendor == "postgresql"
    monkeypatch.setattr(usage, "_meters", {})
    usage.register_usage_meter(M, label="Mensagens", unit_label="mensagem")
    scenario = build_service_scenario(Note, slug=f"billing-usage-race-{int(seeded)}")
    settings.SKELETON_BILLING_ENABLED = True
    now = MAR1 + timedelta(days=2)
    subscribe_with_usage(scenario.a, {M: {"included": 2 if seeded else 1}}, now=MAR1)
    if seeded:
        with system_context(reason="test:usage-race-seed", organization_id=scenario.a.pk):
            usage.record(M, idempotency_key="seed", now=now)
    workers = 2
    barrier = Barrier(workers)

    def spend(number):
        connections.close_all()
        try:
            with system_context(reason="test:usage-race", organization_id=scenario.a.pk):
                barrier.wait(10)
                try:
                    return usage.record(M, idempotency_key=f"race:{number}", now=now).used
                except QuotaLimitExceeded:
                    return "refused"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = sorted(pool.map(spend, range(workers), timeout=60), key=str)
    limit = 2 if seeded else 1
    assert results == [limit, "refused"]
    with system_context(reason="test:usage-race-read", organization_id=scenario.a.pk):
        assert UsageBalance.objects.get().used == limit
        assert UsageEvent.objects.count() == limit
        # The winner crossed 80% and 100% once: one notice each, never one per racer (#525).
        from skeleton.notifications.models import Notification

        keys = sorted(
            Notification.objects.filter(type_code="billing.usage_threshold").values_list("dedupe_key", flat=True)
        )
        assert [key.rsplit(":", 1)[1] for key in keys] == ["100", "80"]
