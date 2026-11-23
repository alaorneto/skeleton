"""Organization limits added by each app, set by the platform (#696).

PostgreSQL with the runtime role. The source ``apps`` adds ``AppAvailability.limits`` of every
app **effective** in the organization of the context; only the platform administrator writes
it, with CAS and audit; organization B never gets what an app effective only in A adds.
"""

from uuid import uuid4

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client
from django.urls import reverse

from skeleton.accounts.services import bootstrap_identity
from skeleton.common.exceptions import ConcurrentUpdate, QuotaLimitExceeded
from skeleton.system import applications, availability
from skeleton.tenancy.context import request_context, system_context, tenant_context
from skeleton.testing.applications import activate_app
from skeleton.testing.people import add_member, make_person

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.closed_app_defaults]
PASSWORD = "App-limits-Password!696"
NOTES = "example_notes"


def demote(user):
    with system_context(reason="test:app-limits-demote"):
        user.is_staff = user.is_superuser = False
        user.save(update_fields=["is_staff", "is_superuser"])


@pytest.fixture
def setup():
    platform, org_a = bootstrap_identity(
        email="app-limits-platform@example.test", organization="app-limits-a", name="A", password=PASSWORD
    )
    admin_b, org_b = bootstrap_identity(
        email="app-limits-b@example.test", organization="app-limits-b", name="B", password=PASSWORD
    )
    demote(admin_b)
    admin_b.refresh_from_db()
    return platform, org_a, admin_b, org_b


def as_platform(user, fn):
    with request_context(actor=user, request_id=uuid4(), source="test:app-limits-platform"):
        return fn()


def status(organization, metric="members"):
    from skeleton.system.limits import limit_status

    with system_context(reason="test:app-limits-status", organization_id=organization.pk):
        return limit_status(metric)


def offer(label, limits, mode="all"):
    """An availability row for ``label`` (any string: the source reads rows, not manifests)."""
    from skeleton.system.models import AppAvailability

    with system_context(reason="test:app-limits-offer"):
        AppAvailability(app_label=label, mode=mode, upgrade_state="done", limits=limits).save()


def effective(monkeypatch, per_organization):
    """Pretend which apps are effective in each organization (two apps without two manifests)."""
    from skeleton.tenancy.context import current_context

    def states():
        labels = per_organization.get(current_context().organization_id, ())
        return {label: {"effective": True} for label in labels} | {"app_off": {"effective": False}}

    monkeypatch.setattr(applications, "application_states", states)


def test_validate_app_limits():
    from skeleton.system.limits import validate_app_limits

    assert validate_app_limits({}) == {}
    assert validate_app_limits({"members": 2, "external_people": 0}) == {"members": 2}
    assert list(validate_app_limits({"members": 1, "external_people": 3})) == ["external_people", "members"]
    for invalid in (
        [],
        {"acme.unknown": 1},
        {"members": -1},
        {"members": 100_001},
        {"members": True},
        {"members": "2"},
    ):
        with pytest.raises(ValidationError):
            validate_app_limits(invalid)


def test_effective_apps_add_to_the_base(setup, settings, monkeypatch):
    _, org_a, _, org_b = setup
    settings.SKELETON_ORGANIZATION_MEMBER_LIMIT = 1
    offer("app_x", {"members": 2})
    offer("app_y", {"members": 7, "external_people": 4})
    offer("app_off", {"members": 50})  # Not effective: withdrawn, disabled, unpaid or missing a dependency.
    per_organization = {org_a.pk: ("app_x",)}
    effective(monkeypatch, per_organization)
    only_x = status(org_a)
    assert (only_x.limit, only_x.base, [(c.source, c.amount) for c in only_x.contributions]) == (
        3,
        1,
        [("app:app_x", 2)],
    )
    per_organization[org_a.pk] = ("app_x", "app_y")
    assert status(org_a).limit == 10
    assert status(org_a, "external_people").limit == 4  # No default: the app's amount is the limit.
    per_organization[org_a.pk] = ("app_y",)
    assert status(org_a).limit == 8
    # B has none of them: only the instance default.
    assert (status(org_b).limit, status(org_b).contributions) == (1, ())


@pytest.mark.needs_example_app
def test_example_app_counts_only_while_effective(setup, settings):
    from skeleton.system.applications import configure_application
    from skeleton.system.models import AppInstallation

    platform, org_a, _, org_b = setup
    settings.SKELETON_ORGANIZATION_MEMBER_LIMIT = 1
    as_platform(platform, lambda: availability.set_app_availability(NOTES, user=platform, mode="selected", revision=0))
    as_platform(platform, lambda: availability.set_app_limits(NOTES, user=platform, limits={"members": 2}, revision=1))
    assert status(org_a).limit == 1  # Not offered to A yet.
    as_platform(platform, lambda: availability.grant_app(NOTES, user=platform, organization_id=org_a.pk))
    assert status(org_a).limit == 1  # Offered, not enabled.
    activate_app(NOTES, org_a.pk)
    assert status(org_a).limit == 3
    assert status(org_b).limit == 1  # B is not granted the app.
    ana, bia = (make_person(email=f"{name}@app-limits.example.test", password=PASSWORD) for name in ("ana", "bia"))
    add_member(organization=org_a, user=ana)
    add_member(organization=org_a, user=bia)
    with pytest.raises(QuotaLimitExceeded):
        add_member(organization=org_a, user=make_person(email="caio@app-limits.example.test", password=PASSWORD))

    # The next request sees a new amount, in every organization.
    as_platform(platform, lambda: availability.set_app_limits(NOTES, user=platform, limits={"members": 5}, revision=2))
    assert status(org_a).limit == 6
    # Disabled by the organization: the app no longer adds anything.
    with tenant_context(organization_id=org_a.pk, actor=platform, source="test:app-limits-disable"):
        record = AppInstallation.objects.get(app_label=NOTES)
        configure_application(NOTES, user=platform, enabled=False, configuration={}, revision=record.revision)
    after = status(org_a)
    assert (after.limit, after.over_limit) == (1, True)
    # Withdrawn by the platform: nothing either, even if enabled again.
    activate_app(NOTES, org_a.pk)
    assert status(org_a).limit == 6
    as_platform(platform, lambda: availability.revoke_app(NOTES, user=platform, organization_id=org_a.pk))
    assert status(org_a).limit == 1


@pytest.mark.needs_example_app
def test_set_app_limits_guards_cas_and_audit(setup):
    from skeleton.audit.models import AuditEvent
    from skeleton.system.models import AppAvailability

    platform, _, admin_b, org_b = setup
    with pytest.raises(ValidationError):  # No availability row yet: offer first.
        as_platform(
            platform, lambda: availability.set_app_limits(NOTES, user=platform, limits={"members": 2}, revision=0)
        )
    row = as_platform(platform, lambda: availability.set_app_availability(NOTES, user=platform, mode="all", revision=0))
    with pytest.raises(ValidationError):
        as_platform(platform, lambda: availability.set_app_limits(NOTES, user=platform, limits={"x.y": 2}, revision=1))
    with pytest.raises(ConcurrentUpdate):
        as_platform(
            platform, lambda: availability.set_app_limits(NOTES, user=platform, limits={"members": 2}, revision=0)
        )
    with request_context(actor=admin_b, request_id=uuid4(), source="test:app-limits-denied"):
        with pytest.raises(PermissionDenied):
            availability.set_app_limits(NOTES, user=admin_b, limits={"members": 2}, revision=1)
    with tenant_context(organization_id=org_b.pk, actor=admin_b, source="test:app-limits-org-admin"):
        with pytest.raises(PermissionDenied):
            availability.set_app_limits(NOTES, user=admin_b, limits={"members": 2}, revision=1)
    as_platform(platform, lambda: availability.set_app_limits(NOTES, user=platform, limits={"members": 2}, revision=1))
    # The same value again writes nothing (no revision bump, no audit event).
    as_platform(platform, lambda: availability.set_app_limits(NOTES, user=platform, limits={"members": 2}, revision=2))
    events = as_platform(
        platform,
        lambda: list(
            AuditEvent.objects.filter(record_sysid=row.pk, organization_id__isnull=True)
            .order_by("record_revision")
            .values_list("operation", "actor_sysid")
        ),
    )
    assert events == [("create", platform.pk), ("update", platform.pk)]
    with system_context(reason="test:app-limits-read"):
        assert AppAvailability.objects.get(pk=row.pk).limits == {"members": 2}


def login(user):
    client = Client(enforce_csrf_checks=True)
    assert client.get(reverse("account_login")).status_code == 200
    response = client.post(
        reverse("account_login"),
        {"login": user.email, "password": PASSWORD, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
    )
    assert response.status_code == 302
    return client


@pytest.mark.needs_example_app
def test_platform_screen_sets_the_amounts(setup, settings):
    from skeleton.system.models import AppAvailability

    platform, _, admin_b, _ = setup
    client = login(platform)
    url = reverse("platform:app-limits", kwargs={"app_label": NOTES})
    page = reverse("platform:app-availability", kwargs={"app_label": NOTES})

    def post(data, who=client):
        return who.post(url, {**data, "csrfmiddlewaretoken": who.cookies["csrftoken"].value})

    response = client.get(page)
    form = response.context["limits_form"]
    fields = {code: name for name, code in form.metrics.items()}
    members = fields["members"]
    assert post({members: 2, "expected_revision": 0}).status_code == 400  # No availability yet.
    as_platform(platform, lambda: availability.set_app_availability(NOTES, user=platform, mode="all", revision=0))
    assert post({members: -1, "expected_revision": 1}).status_code == 400
    assert post({members: 100_001, "expected_revision": 1}).status_code == 400
    assert post({members: 2, "expected_revision": 0}).status_code == 409  # Stale revision.
    settings.SKELETON_ORGANIZATION_MEMBER_LIMIT = None
    assert post({members: 2, "expected_revision": 1}).status_code == 302
    with system_context(reason="test:app-limits-screen"):
        assert AppAvailability.objects.get(app_label=NOTES).limits == {"members": 2}
    shown = client.get(page)
    assert shown.context["limits_form"][members].value() == 2
    assert shown.context["limits_without_base"] is True
    assert "este acréscimo vira o limite inteiro" in shown.content.decode()
    settings.SKELETON_ORGANIZATION_MEMBER_LIMIT = 1
    assert client.get(page).context["limits_without_base"] is False
    # An organization administrator is not the platform.
    other = login(admin_b)
    assert post({members: 9, "expected_revision": 2}, who=other).status_code in (403, 404)
    with system_context(reason="test:app-limits-screen"):
        assert AppAvailability.objects.get(app_label=NOTES).limits == {"members": 2}
