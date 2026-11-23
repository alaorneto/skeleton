"""Two admissions for the last seat of an organization: exactly one wins (#695)."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from django.db import connection, connections, transaction

from tools.integration_database import disposable_database_pair
from tools.security_fixtures import build_security_fixture


def test_two_admissions_for_the_last_seat_one_is_refused(skeleton_db, settings):
    from skeleton.access.models import Role
    from skeleton.access.services import provision_membership
    from skeleton.common.exceptions import QuotaLimitExceeded
    from skeleton.system.limits import limit_status
    from skeleton.tenancy.context import system_context
    from skeleton.testing.people import make_person

    disposable_database_pair()
    assert connection.vendor == "postgresql"
    fixture = build_security_fixture()
    newcomers = [make_person(email=f"seat{n}@member-limit.example.test", password=None) for n in range(2)]
    with system_context(reason="test:seat-count", organization_id=fixture.a.pk):
        settings.SKELETON_ORGANIZATION_MEMBER_LIMIT = limit_status("members").used + 1  # One free seat.
    barrier = Barrier(2)

    def attempt(user):
        connections.close_all()
        try:
            with transaction.atomic(), system_context(reason="test:seat-race", organization_id=fixture.a.pk):
                role = Role.objects.get(code="member")
                barrier.wait(10)
                try:
                    provision_membership(organization=fixture.a, user=user, role=role)
                except QuotaLimitExceeded:
                    return "refused"
                return "admitted"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(attempt, newcomers, timeout=60))
    assert outcomes == ["admitted", "refused"]
    with system_context(reason="test:seat-after", organization_id=fixture.a.pk):
        status = limit_status("members")
        assert status.used == status.limit
