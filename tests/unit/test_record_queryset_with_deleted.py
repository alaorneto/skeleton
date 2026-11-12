"""with_deleted() removes every live-row filter of the manager, also in combined querysets (#274)."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.db.models import Q

LIVE = '"deleted_at" IS NULL'


@pytest.fixture
def tenant(monkeypatch):
    """A tenant for the manager filter, without a database: the tests only compile SQL."""
    active = SimpleNamespace(organization_id=uuid4())
    monkeypatch.setattr("skeleton.common.querysets.context", lambda: active)
    return active


def sql(queryset):
    return str(queryset.query)


def test_plain_queryset_sql_is_unchanged_and_with_deleted_removes_the_filter(tenant):
    from skeleton.access.models import Role

    queryset = Role.objects.filter(code="a")
    assert sql(queryset).count(LIVE) == 1
    assert sql(queryset.with_deleted()).count(LIVE) == 0
    assert sql(queryset).count(LIVE) == 1  # The original is not changed.
    assert str(tenant.organization_id) in sql(queryset.with_deleted())


@pytest.mark.parametrize(
    "combine",
    [
        lambda m: m.objects.filter(code="a") | m.objects.filter(code="b"),
        lambda m: m.objects.filter(code="a") & m.objects.filter(name="x"),
        lambda m: m.objects.filter(Q(code="a") | Q(name="x")).exclude(code="z"),
    ],
    ids=["or", "and", "q-exclude"],
)
def test_combined_querysets_lose_every_live_filter_but_keep_the_tenant(tenant, combine):
    from skeleton.access.models import Role

    combined = combine(Role)
    assert sql(combined).count(LIVE) >= 1
    widened = combined.with_deleted()
    assert sql(widened).count(LIVE) == 0
    assert str(tenant.organization_id) in sql(widened)
    assert sql(combined).count(LIVE) >= 1


def test_explicit_deleted_at_filter_of_the_caller_is_kept(tenant):
    from skeleton.access.models import Role

    queryset = Role.objects.with_deleted().filter(deleted_at__isnull=True)
    assert sql(queryset.with_deleted()).count(LIVE) == 1
