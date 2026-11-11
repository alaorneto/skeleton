"""Concurrent reservations above the storage quota: exactly one wins (#61)."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import connection, connections

from tools.integration_database import disposable_database_pair
from tools.security_fixtures import actor_tenant, build_security_fixture


@pytest.mark.parametrize("existing", [False, True], ids=["first-reservation", "existing-usage"])
def test_two_reservations_above_the_quota_one_fails(skeleton_db, settings, existing):
    from skeleton.files.models import StorageUsage
    from skeleton.files.quota import QuotaExceeded, reserve

    disposable_database_pair()
    assert connection.vendor == "postgresql"
    settings.SKELETON_FILES_ORG_QUOTA_BYTES = 150
    fixture = build_security_fixture()
    if existing:
        with actor_tenant(fixture.admin, fixture.a):
            reserve(fixture.a.pk, 10)
    barrier = Barrier(2)

    def attempt(user):
        connections.close_all()
        try:
            with actor_tenant(user, fixture.a):
                barrier.wait(10)
                try:
                    reserve(fixture.a.pk, 100)
                except QuotaExceeded:
                    return "refused"
                return "reserved"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(attempt, [fixture.alice, fixture.bob], timeout=60))
    assert outcomes == ["refused", "reserved"]
    with actor_tenant(fixture.admin, fixture.a):
        assert StorageUsage.objects.get(organization_id=fixture.a.pk).bytes_used == (110 if existing else 100)
    with actor_tenant(fixture.bob, fixture.b):
        assert not StorageUsage.objects.exists()  # RLS: A's counter is invisible from B.
