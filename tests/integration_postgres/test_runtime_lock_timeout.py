"""A runtime transaction that waits too long for a lock fails and writes nothing (#318)."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from django.db import OperationalError, connection, connections

from tools.integration_database import disposable_database_pair
from tools.security_fixtures import actor_tenant, build_security_fixture

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]


@pytest.fixture
def fixture():
    disposable_database_pair()
    assert connection.vendor == "postgresql"
    return build_security_fixture()


def _isolated(function):
    def run(*args):
        connections.close_all()
        try:
            return function(*args)
        finally:
            connections.close_all()

    return run


def _audit_count():
    from skeleton.audit.models import AuditEvent
    from skeleton.tenancy.context import system_context

    with system_context(reason="test:lock-timeout-audit"):
        return AuditEvent.objects.count()


def test_blocked_update_fails_with_lock_timeout_and_writes_nothing(fixture):
    from examples.notes.models import Note
    from skeleton.access.services import organization_lock

    locked, release = Event(), Event()

    @_isolated
    def holder():
        with actor_tenant(fixture.admin, fixture.a), organization_lock(fixture.admin):
            locked.set()
            assert release.wait(20)

    @_isolated
    def blocked():
        with actor_tenant(fixture.admin, fixture.a):
            with connection.cursor() as cursor:
                # The role default is 10s; a shorter local value keeps the test fast.
                cursor.execute("SET LOCAL lock_timeout = '1s'")
            Note(organization=fixture.a, owner=fixture.admin, code="blocked", title="t", body="").save()
            with organization_lock(fixture.admin):
                pass

    before = _audit_count()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(holder)
        try:
            assert locked.wait(20)
            with pytest.raises(OperationalError) as raised:
                blocked()
        finally:
            release.set()
        pending.result(timeout=30)
    assert getattr(raised.value.__cause__, "sqlstate", None) == "55P03"  # lock_not_available
    with actor_tenant(fixture.admin, fixture.a):
        assert not Note.objects.filter(code="blocked").exists()
    assert _audit_count() == before
