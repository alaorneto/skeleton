"""Platform availability of installed apps above each organization's activation (#21)."""

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import models
from django.test import Client
from django.test.utils import isolate_apps
from django.urls import reverse

from skeleton.accounts.services import bootstrap_identity
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.system import availability
from skeleton.system.applications import application_state, configure_application, navigation_for_user
from skeleton.tenancy.context import request_context, system_context, tenant_context
from skeleton.testing.applications import activate_app

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.closed_app_defaults]
PASSWORD = "Availability-gate-Password!61"
NOTES = "example_notes"


def require_notes():
    from django.apps import apps

    if not apps.is_installed("examples.notes"):
        pytest.skip("SKELETON_ENABLE_EXAMPLE_APP=true is required")


def login(user):
    client = Client()
    assert client.post(reverse("account_login"), {"login": user.email, "password": PASSWORD}).status_code == 302
    return client


def demote(user):
    with system_context(reason="test:availability-demote"):
        user.is_staff = user.is_superuser = False
        user.save(update_fields=["is_staff", "is_superuser"])


@pytest.fixture
def setup():
    require_notes()
    platform, org_a = bootstrap_identity(
        email="availability-platform@example.test", organization="availability-a", name="A", password=PASSWORD
    )
    member_b, org_b = bootstrap_identity(
        email="availability-b@example.test", organization="availability-b", name="B", password=PASSWORD
    )
    demote(member_b)
    member_b.refresh_from_db()
    return platform, org_a, member_b, org_b


def as_platform(user, fn):
    with request_context(actor=user, request_id=None, source="test:availability-platform"):
        return fn()


def test_selected_app_is_closed_for_organizations_without_grant(setup):
    platform, org_a, member_b, org_b = setup
    as_platform(platform, lambda: availability.set_app_availability(NOTES, user=platform, mode="selected", revision=0))
    as_platform(platform, lambda: availability.grant_app(NOTES, user=platform, organization_id=org_a.pk))
    with tenant_context(organization_id=org_a.pk, actor=platform, source="test:availability-a"):
        assert application_state(NOTES)["available"] is True
    activate_app(NOTES, org_b.pk)  # B enabled the app, but the platform gate stays closed.
    with tenant_context(organization_id=org_b.pk, actor=member_b, source="test:availability-b"):
        state = application_state(NOTES)
        assert state["available"] is False and state["enabled"] is True
        assert availability.availability_map([NOTES, "unknown"]) == {NOTES: False, "unknown": False}
        assert all(entry["app_label"] != NOTES for entry in navigation_for_user(member_b))
    client = login(member_b)
    records = {"organization_id": org_b.pk, "table_key": "example_notes.note"}
    assert client.get(reverse("example_notes:home", kwargs={"organization_id": org_b.pk})).status_code == 404
    assert client.get(reverse("api:records", kwargs=records)).status_code in (403, 404)
    assert client.get(reverse("web:records", kwargs=records)).status_code in (403, 404)
    catalog = client.get(reverse("api:catalog", kwargs={"organization_id": org_b.pk})).json()
    assert all(row["table_key"] != "example_notes.note" for row in catalog["results"])


def test_revoke_applies_on_next_request_and_preserves_activation(setup):
    from skeleton.system.models import AppInstallation

    platform, org_a, _, _ = setup
    demote_target = platform
    as_platform(platform, lambda: availability.set_app_availability(NOTES, user=platform, mode="selected", revision=0))
    as_platform(platform, lambda: availability.grant_app(NOTES, user=platform, organization_id=org_a.pk))
    with tenant_context(organization_id=org_a.pk, actor=platform, source="test:availability-configure"):
        state = application_state(NOTES)
        configure_application(
            NOTES, user=platform, enabled=True, configuration=state["configuration"], revision=state["revision"]
        )
    client = login(demote_target)
    home = reverse("example_notes:home", kwargs={"organization_id": org_a.pk})
    assert client.get(home).status_code == 200
    first = as_platform(platform, lambda: availability.revoke_app(NOTES, user=platform, organization_id=org_a.pk))
    assert client.get(home).status_code == 404
    with tenant_context(organization_id=org_a.pk, actor=platform, source="test:availability-kept"):
        assert AppInstallation.objects.get(app_label=NOTES).enabled is True
    again = as_platform(platform, lambda: availability.grant_app(NOTES, user=platform, organization_id=org_a.pk))
    assert again.pk == first.pk and again.deleted_at is None
    assert client.get(home).status_code == 200


def test_configure_refuses_to_enable_unavailable_app_but_allows_disabling(setup):
    platform, org_a, _, _ = setup
    as_platform(platform, lambda: availability.set_app_availability(NOTES, user=platform, mode="selected", revision=0))
    with tenant_context(organization_id=org_a.pk, actor=platform, source="test:availability-refuse"):
        state = application_state(NOTES)
        with pytest.raises(ValidationError) as error:
            configure_application(
                NOTES, user=platform, enabled=True, configuration=state["configuration"], revision=state["revision"]
            )
        assert error.value.error_dict["enabled"][0].code == "app_unavailable"
        record = configure_application(
            NOTES, user=platform, enabled=False, configuration=state["configuration"], revision=state["revision"]
        )
        assert record.enabled is False


def test_dependencies_gate_grant_and_revoke(setup, monkeypatch):
    platform, org_a, _, _ = setup
    monkeypatch.setattr(availability, "required_labels", lambda label: ("base_app",) if label == NOTES else ())
    monkeypatch.setattr(availability, "dependent_labels", lambda label: (NOTES,) if label == "base_app" else ())
    from skeleton.system.models import AppAvailability, AppOrganizationGrant

    def closed_base():
        with system_context(reason="test:availability-base"):
            AppAvailability(app_label="base_app", mode="selected").save()

    closed_base()
    with pytest.raises(ValidationError) as error:
        as_platform(platform, lambda: availability.grant_app(NOTES, user=platform, organization_id=org_a.pk))
    assert error.value.error_dict["app_label"][0].code == "dependency_unavailable"
    with system_context(reason="test:availability-base-grant"):
        base = AppAvailability.objects.get(app_label="base_app")
        AppOrganizationGrant(availability=base, granted_organization=org_a).save()
    as_platform(platform, lambda: availability.set_app_availability(NOTES, user=platform, mode="selected", revision=0))
    as_platform(platform, lambda: availability.grant_app(NOTES, user=platform, organization_id=org_a.pk))
    monkeypatch.setattr(availability, "get_app", lambda label: None)
    with pytest.raises(ValidationError) as error:
        as_platform(platform, lambda: availability.revoke_app("base_app", user=platform, organization_id=org_a.pk))
    assert error.value.error_dict["app_label"][0].code == "dependent_granted"


def test_availability_cas_has_one_winner_and_is_audited(setup):
    from skeleton.audit.models import AuditEvent

    platform, _, _, _ = setup
    row = as_platform(
        platform, lambda: availability.set_app_availability(NOTES, user=platform, mode="selected", revision=0)
    )
    with pytest.raises(ConcurrentUpdate):
        as_platform(platform, lambda: availability.set_app_availability(NOTES, user=platform, mode="all", revision=0))
    as_platform(platform, lambda: availability.set_app_availability(NOTES, user=platform, mode="all", revision=1))
    with pytest.raises(ConcurrentUpdate):
        as_platform(
            platform, lambda: availability.set_app_availability(NOTES, user=platform, mode="selected", revision=1)
        )
    # Global events are readable only with the platform GUC on (RLS in PostgreSQL).
    operations = as_platform(
        platform,
        lambda: list(
            AuditEvent.objects.filter(record_sysid=row.pk, organization_id__isnull=True)
            .order_by("record_revision")
            .values_list("operation", flat=True)
        ),
    )
    assert operations == ["create", "update"]


def test_only_platform_administrators_change_availability(setup):
    _, org_a, member_b, org_b = setup
    with request_context(actor=member_b, request_id=None, source="test:availability-denied"):
        with pytest.raises(PermissionDenied):
            availability.set_app_availability(NOTES, user=member_b, mode="selected", revision=0)
    with tenant_context(organization_id=org_b.pk, actor=member_b, source="test:availability-org-admin"):
        with pytest.raises(PermissionDenied):
            availability.grant_app(NOTES, user=member_b, organization_id=org_b.pk)


def test_e017_rejects_system_record_with_organization_field():
    from skeleton.common.models import SystemRecord
    from skeleton.system.checks import record_contract_checks

    with isolate_apps("skeleton.system.apps.SystemConfig") as registry:

        class Leaky(SystemRecord):
            organization = models.ForeignKey("sk_tenancy.Organization", on_delete=models.PROTECT)

            class Meta(SystemRecord.Meta):
                app_label = "sk_system"

        ids = {error.id for error in record_contract_checks(apps_registry=registry)}
    assert "skeleton.E017" in ids
