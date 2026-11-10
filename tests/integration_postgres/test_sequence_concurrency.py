"""Concurrent allocations of one sequence get distinct, consecutive numbers without deadlock (#155)."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier

import pytest
from django.db import connection, connections

from skeleton.sequences import registry
from skeleton.sequences.registry import SequenceSpec, register_sequence
from tools.integration_database import disposable_database_pair
from tools.security_fixtures import actor_tenant, build_security_fixture

KEY = "example_notes.ticket"
AT = datetime(2026, 5, 1, tzinfo=UTC)


@pytest.mark.parametrize("existing", [False, True], ids=["first-creation", "existing-counter"])
def test_concurrent_allocations_are_consecutive(skeleton_db, monkeypatch, existing):
    from skeleton.sequences.models import SequenceCounter
    from skeleton.sequences.services import next_number

    disposable_database_pair()
    assert connection.vendor == "postgresql"
    monkeypatch.setattr(registry, "_sequences", dict(registry._sequences))
    register_sequence(SequenceSpec(KEY, template="AT-{year}-{number:04d}"))
    fixture = build_security_fixture()
    if existing:
        with actor_tenant(fixture.admin, fixture.a):
            next_number(KEY, at=AT)
    workers = 6
    barrier = Barrier(workers)

    def allocate(user):
        connections.close_all()
        try:
            with actor_tenant(user, fixture.a):
                barrier.wait(10)
                return next_number(KEY, at=AT).number
        finally:
            connections.close_all()

    users = [fixture.admin, fixture.alice, fixture.bob] * 2
    with ThreadPoolExecutor(max_workers=workers) as pool:
        numbers = sorted(pool.map(allocate, users, timeout=60))
    start = 2 if existing else 1
    assert numbers == list(range(start, start + workers))
    with actor_tenant(fixture.admin, fixture.a):
        assert SequenceCounter.objects.get(key=KEY, period="2026").last_value == start + workers - 1
    # RLS: the counter of A is invisible from B.
    with actor_tenant(fixture.bob, fixture.b):
        assert not SequenceCounter.objects.exists()
