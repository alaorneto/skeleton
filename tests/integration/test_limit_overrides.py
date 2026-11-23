"""The platform's adjustment of one organization limit (#697): bonus, fixed limit, expiry.

PostgreSQL with the runtime role. A fixed limit replaces the calculation (empty: unlimited); a
bonus adds to it; an expired adjustment is ignored without any job; removing it returns to the
calculation and setting it again restores the same row. Only the platform administrator.
"""

from datetime import timedelta
from uuid import uuid4

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from skeleton.accounts.services import bootstrap_identity
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.system import applications
from skeleton.system.limits import limit_breakdown, remove_limit_override, set_limit_override
from skeleton.tenancy.context import request_context, system_context, tenant_context

pytestmark = [pytest.mark.usefixtures("skeleton_db")]
PASSWORD = "Limit-overrides-Password!697"


def demote(user):
    with system_context(reason="test:limit-overrides-demote"):
        user.is_staff = user.is_superuser = False
        user.save(update_fields=["is_staff", "is_superuser"])


@pytest.fixture
def setup():
    platform, org_a = bootstrap_identity(
        email="overrides-platform@example.test", organization="overrides-a", name="A", password=PASSWORD
    )
    admin_b, org_b = bootstrap_identity(
        email="overrides-b@example.test", organization="overrides-b", name="B", password=PASSWORD
    )
    demote(admin_b)
    admin_b.refresh_from_db()
    return platform, org_a, admin_b, org_b


def as_platform(user, fn):
    with request_context(actor=user, request_id=uuid4(), source="test:limit-overrides"):
        return fn()


def status(organization, metric="members"):
    from skeleton.system.limits import limit_status

    with system_context(reason="test:limit-overrides-status", organization_id=organization.pk):
        return limit_status(metric)


def give(platform, organization, **kwargs):
    kwargs.setdefault("metric", "members")
    kwargs.setdefault("reason", "Contrato negociado")
    return as_platform(platform, lambda: set_limit_override(actor=platform, organization=organization, **kwargs))


def test_bonus_fixed_and_unlimited(setup, settings, monkeypatch):
    from skeleton.system.models import AppAvailability

    platform, org_a, _, org_b = setup
    settings.SKELETON_ORGANIZATION_MEMBER_LIMIT = 1
    with system_context(reason="test:limit-overrides-offer"):
        AppAvailability(app_label="app_x", mode="all", upgrade_state="done", limits={"members": 2}).save()
    monkeypatch.setattr(applications, "application_states", lambda: {"app_x": {"effective": True}})
    assert status(org_a).limit == 3
    row = give(platform, org_a, mode="bonus", value=5)
    bonus = status(org_a)
    assert (bonus.limit, bonus.fixed, [c.source for c in bonus.contributions]) == (8, False, ["app:app_x", "platform"])
    row = give(platform, org_a, mode="fixed", value=50, expected_revision=row.revision)
    fixed = status(org_a)
    assert (fixed.limit, fixed.fixed) == (50, True)
    row = give(platform, org_a, mode="fixed", value=None, expected_revision=row.revision)
    assert status(org_a).limit is None  # Fixed and empty: unlimited.
    assert status(org_b).limit == 3  # A's adjustment never reaches B.


def test_expired_adjustment_is_ignored(setup, settings):
    from skeleton.system.models import OrganizationLimitOverride

    platform, org_a, _, _ = setup
    settings.SKELETON_ORGANIZATION_MEMBER_LIMIT = 1
    row = give(platform, org_a, mode="bonus", value=4, expires_at=timezone.now() + timedelta(days=1))
    assert status(org_a).limit == 5
    with system_context(reason="test:limit-overrides-expire"):
        # Moving the expiry to the past without the service (which refuses a past date).
        OrganizationLimitOverride.objects.filter(pk=row.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
    assert status(org_a).limit == 1
    [members] = [
        item
        for item in as_platform(platform, lambda: limit_breakdown(actor=platform, organization=org_a))
        if item.status.metric == "members"
    ]
    assert members.override.pk == row.pk and members.expired


def test_remove_and_restore_keep_the_row(setup, settings):
    from skeleton.audit.models import AuditEvent

    platform, org_a, _, _ = setup
    settings.SKELETON_ORGANIZATION_MEMBER_LIMIT = 1
    row = give(platform, org_a, mode="bonus", value=2)
    with pytest.raises(ConcurrentUpdate):
        as_platform(
            platform,
            lambda: remove_limit_override(
                actor=platform, organization=org_a, metric="members", expected_revision=row.revision + 1
            ),
        )
    as_platform(
        platform,
        lambda: remove_limit_override(
            actor=platform, organization=org_a, metric="members", expected_revision=row.revision
        ),
    )
    assert status(org_a).limit == 1
    with pytest.raises(ConcurrentUpdate):  # Removed: only revision 0 sets it again.
        give(platform, org_a, mode="bonus", value=3, expected_revision=row.revision)
    again = give(platform, org_a, mode="bonus", value=3)
    assert again.pk == row.pk and status(org_a).limit == 4
    operations = as_platform(
        platform,
        lambda: list(
            AuditEvent.objects.filter(record_sysid=row.pk, organization_id__isnull=True)
            .order_by("record_revision")
            .values_list("operation", "actor_sysid")
        ),
    )
    assert [operation for operation, _ in operations] == ["create", "soft_delete", "restore", "update"]
    assert {actor for _, actor in operations} == {platform.pk}


def test_invalid_adjustments_write_nothing(setup):
    from skeleton.system.models import OrganizationLimitOverride

    platform, org_a, _, _ = setup
    invalid = [
        {"mode": "bonus", "value": 2, "reason": "  "},
        {"mode": "bonus", "value": 2, "metric": "acme.unknown"},
        {"mode": "bonus", "value": 0},
        {"mode": "bonus", "value": None},
        {"mode": "other", "value": 2},
        {"mode": "fixed", "value": 2, "expires_at": timezone.now() - timedelta(days=1)},
    ]
    for kwargs in invalid:
        with pytest.raises(ValidationError):
            give(platform, org_a, **kwargs)
    row = give(platform, org_a, mode="bonus", value=2)
    with pytest.raises(ConcurrentUpdate):  # Stale revision.
        give(platform, org_a, mode="bonus", value=9, expected_revision=0)
    with system_context(reason="test:limit-overrides-read"):
        assert list(OrganizationLimitOverride.objects.values_list("pk", "value")) == [(row.pk, 2)]


def test_only_the_platform_administrator(setup):
    platform, org_a, admin_b, org_b = setup
    with request_context(actor=admin_b, request_id=uuid4(), source="test:limit-overrides-denied"):
        with pytest.raises(PermissionDenied):
            set_limit_override(actor=admin_b, organization=org_b, metric="members", mode="bonus", value=1, reason="x")
        with pytest.raises(PermissionDenied):
            limit_breakdown(actor=admin_b, organization=org_b)
    with tenant_context(organization_id=org_b.pk, actor=admin_b, source="test:limit-overrides-org-admin"):
        with pytest.raises(PermissionDenied):
            set_limit_override(actor=admin_b, organization=org_b, metric="members", mode="bonus", value=1, reason="x")


def login(user):
    client = Client(enforce_csrf_checks=True)
    assert client.get(reverse("account_login")).status_code == 200
    response = client.post(
        reverse("account_login"),
        {"login": user.email, "password": PASSWORD, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
    )
    assert response.status_code == 302
    return client


def test_platform_page_shows_and_sets_adjustments(setup, settings):
    platform, org_a, admin_b, _ = setup
    settings.SKELETON_ORGANIZATION_MEMBER_LIMIT = 1
    client = login(platform)
    url = reverse("platform:organization-limits", kwargs={"sysid": org_a.pk})

    def post(data, who=client):
        return who.post(url, {**data, "csrfmiddlewaretoken": who.cookies["csrftoken"].value})

    page = client.get(url)
    assert page.status_code == 200
    members = next(item["row"] for item in page.context["rows"] if item["row"].status.metric == "members")
    assert (members.status.limit, members.status.used, members.override) == (1, 1, None)
    assert (
        reverse("platform:organization-limits", kwargs={"sysid": org_a.pk})
        in client.get(reverse("platform:organization", kwargs={"sysid": org_a.pk})).content.decode()
    )
    base = {"metric": "members", "mode": "bonus", "value": 5, "reason": "Piloto", "expected_revision": 0}
    assert post({**base, "reason": ""}).status_code == 400
    assert post({**base, "value": ""}).status_code == 400  # A bonus needs a value.
    assert post({**base, "metric": "acme.unknown"}).status_code == 400
    assert post(base).status_code == 302
    assert status(org_a).limit == 6
    assert post({**base, "value": 7}).status_code == 409  # Revision 0 again: stale.
    edit = client.get(url, {"metric": "members"}).context["form"]
    assert (edit["value"].value(), edit["expected_revision"].value()) == (5, 1)
    assert post({**base, "mode": "fixed", "value": "", "expected_revision": 1}).status_code == 302
    assert status(org_a).limit is None
    assert post({"action": "remove", "metric": "members", "expected_revision": 1}).status_code == 409
    assert post({"action": "remove", "metric": "members", "expected_revision": 2}).status_code == 302
    assert status(org_a).limit == 1
    # An organization administrator is not the platform: nothing to see.
    other = login(admin_b)
    assert other.get(url).status_code in (403, 404)
    assert post(base, who=other).status_code in (403, 404)
