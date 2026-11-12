"""The organization row lock is FOR NO KEY UPDATE: exclusive among locks, compatible with inserts (#239)."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier, Event

import pytest
from django.db import OperationalError, connection, connections

from skeleton.testing.registries import table_spec_override
from tools.integration_database import disposable_database_pair
from tools.security_fixtures import actor_tenant, build_security_fixture

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]
KEY = "example_notes.lock_probe"
AT = datetime(2026, 5, 1, tzinfo=UTC)


@pytest.fixture
def fixture(monkeypatch):
    from skeleton.sequences import registry
    from skeleton.sequences.registry import SequenceSpec, register_sequence

    disposable_database_pair()
    assert connection.vendor == "postgresql"
    monkeypatch.setattr(registry, "_sequences", dict(registry._sequences))
    register_sequence(SequenceSpec(KEY, template="LP-{year}-{number:04d}"))
    return build_security_fixture()


def _note(fixture, code):
    from examples.notes.models import Note

    Note(organization=fixture.a, owner=fixture.admin, code=code, title="t", body="").save()


def _lock_timeout(seconds):
    with connection.cursor() as cursor:
        cursor.execute(f"SET LOCAL lock_timeout = '{int(seconds)}s'")


def _isolated(function):
    def run(*args):
        connections.close_all()
        try:
            return function(*args)
        finally:
            connections.close_all()

    return run


def test_first_number_after_inserts_does_not_deadlock(fixture):
    from skeleton.sequences.services import next_number

    barrier = Barrier(2)

    @_isolated
    def allocate(code):
        with actor_tenant(fixture.admin, fixture.a):
            _note(fixture, code)
            barrier.wait(10)
            return next_number(KEY, at=AT).number

    with ThreadPoolExecutor(max_workers=2) as pool:
        numbers = sorted(pool.map(allocate, ["lock-1", "lock-2"], timeout=60))
    assert numbers == [1, 2]


def _while_a_transaction_inserted(fixture, action):
    inserted, release = Event(), Event()

    @_isolated
    def holder():
        with actor_tenant(fixture.admin, fixture.a):
            _note(fixture, "holder")
            inserted.set()
            assert release.wait(20)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(holder)
        try:
            assert inserted.wait(20)
            _isolated(action)()
        finally:
            release.set()
        pending.result(timeout=30)


def test_organization_lock_does_not_wait_for_inserts(fixture):
    from skeleton.access.services import organization_lock

    def lock():
        with actor_tenant(fixture.admin, fixture.a):
            _lock_timeout(3)
            with organization_lock(fixture.admin):
                pass

    _while_a_transaction_inserted(fixture, lock)


def test_quota_reservation_does_not_wait_for_inserts(fixture):
    from skeleton.files import quota
    from skeleton.system.registry import AttachmentPolicy

    def reserve():
        with table_spec_override(
            "example_notes.note", attachments=AttachmentPolicy(max_bytes=100, types=("text/plain",))
        ):
            with actor_tenant(fixture.admin, fixture.a):
                _lock_timeout(3)
                quota.reserve(fixture.a.pk, 1)

    _while_a_transaction_inserted(fixture, reserve)


def test_two_organization_locks_still_exclude_each_other(fixture):
    from skeleton.access.services import organization_lock

    locked, release = Event(), Event()

    @_isolated
    def first():
        with actor_tenant(fixture.admin, fixture.a), organization_lock(fixture.admin):
            locked.set()
            assert release.wait(20)

    @_isolated
    def second():
        with actor_tenant(fixture.admin, fixture.a):
            _lock_timeout(1)
            with organization_lock(fixture.admin):
                pass

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(first)
        try:
            assert locked.wait(20)
            with pytest.raises(OperationalError):
                second()
        finally:
            release.set()
        pending.result(timeout=30)


def test_inserts_commit_while_the_organization_is_locked(fixture):
    """Django's deferred FK checks take FOR KEY SHARE on the organization at commit (#239)."""
    from skeleton.access.services import organization_lock

    locked, release = Event(), Event()

    @_isolated
    def holder():
        with actor_tenant(fixture.admin, fixture.a), organization_lock(fixture.admin):
            locked.set()
            assert release.wait(20)

    @_isolated
    def insert_and_commit():
        with actor_tenant(fixture.admin, fixture.a):
            _lock_timeout(2)
            _note(fixture, "committed-while-locked")

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(holder)
        try:
            assert locked.wait(20)
            insert_and_commit()
        finally:
            release.set()
        pending.result(timeout=30)
