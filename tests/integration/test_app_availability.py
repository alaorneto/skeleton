"""Platform availability of installed apps above each organization's activation (#21)."""

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import models
from django.test.utils import isolate_apps
from django.urls import reverse

from skeleton.accounts.services import bootstrap_identity
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.system import availability
from skeleton.system.applications import application_state, configure_application, navigation_for_user
from skeleton.tenancy.context import request_context, system_context, tenant_context
from skeleton.testing.applications import activate_app
from tests import support

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.closed_app_defaults]
PASSWORD = "Availability-gate-Password!61"
NOTES = "example_notes"


def login(user):
    return support.login(user, password=PASSWORD, csrf=False)


def demote(user):
    with system_context(reason="test:availability-demote"):
        user.is_staff = user.is_superuser = False
        user.save(update_fields=["is_staff", "is_superuser"])


@pytest.fixture
def setup():
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


@pytest.mark.needs_example_app
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
    assert client.get(reverse("api:records", kwargs=records)).status_code == 404
    assert client.get(reverse("web:records", kwargs=records)).status_code == 404
    catalog = client.get(reverse("api:catalog", kwargs={"organization_id": org_b.pk})).json()
    assert all(row["table_key"] != "example_notes.note" for row in catalog["results"])


@pytest.mark.needs_example_app
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


@pytest.mark.needs_example_app
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


@pytest.mark.needs_example_app
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


@pytest.mark.needs_example_app
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


@pytest.mark.needs_example_app
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


# --------------------------------------------------------------------------- custom screens of closed apps (#253)


def _with_custom_screen(monkeypatch):
    from dataclasses import replace

    from skeleton.system import app_registry
    from skeleton.system.presentation import AdminScreenSpec, AdminSectionSpec

    manifest = app_registry.get_app(NOTES)
    administration = replace(
        manifest.administration,
        sections=(*manifest.administration.sections, AdminSectionSpec("parameters", "Parameters")),
        screens=(
            *manifest.administration.screens,
            AdminScreenSpec(
                "custom",
                "parameters",
                "Custom parameters",
                "organization",
                "custom",
                url_name="example_notes:parameters",
                organization_admin_only=True,
            ),
        ),
    )
    custom = replace(manifest, administration=administration)
    monkeypatch.setitem(app_registry._manifests, NOTES, custom)
    return custom


@pytest.mark.needs_example_app
def test_custom_parameter_screen_is_closed_when_app_unavailable(setup, monkeypatch):
    from skeleton.common.exceptions import ApplicationUnavailable
    from skeleton.system.administration import resolve_application_screen

    platform, _, member_b, org_b = setup
    _with_custom_screen(monkeypatch)
    with tenant_context(organization_id=org_b.pk, actor=member_b, source="test:custom-closed"):
        with pytest.raises(ApplicationUnavailable):
            resolve_application_screen(NOTES, "custom", user=member_b, scope="organization")
    as_platform(platform, lambda: availability.set_app_availability(NOTES, user=platform, mode="all", revision=0))
    with tenant_context(organization_id=org_b.pk, actor=member_b, source="test:custom-open"):
        manifest, screen = resolve_application_screen(NOTES, "custom", user=member_b, scope="organization")
    assert screen.id == "custom"


@pytest.mark.needs_example_app
def test_unavailable_app_lists_only_core_configuration_screen(setup, monkeypatch):
    from skeleton.system.administration import screen_sections

    _, _, member_b, org_b = setup
    manifest = _with_custom_screen(monkeypatch)
    with tenant_context(organization_id=org_b.pk, actor=member_b, source="test:custom-sections"):
        sections = screen_sections(manifest, user=member_b, scope="organization")
    screens = [screen["id"] for section in sections for screen in section["screens"]]
    assert "configuration" in screens and "custom" not in screens


@pytest.mark.needs_example_app
def test_cli_refuses_enabling_app_with_closed_dependency(setup, monkeypatch):
    """The command applies the activation rule of configure_application (#282)."""
    from io import StringIO

    from django.core.management import call_command
    from django.core.management.base import CommandError

    from skeleton.system.models import AppInstallation

    _, _, _, org_b = setup
    monkeypatch.setattr(availability, "required_labels", lambda label: ("base_app",) if label == NOTES else ())
    with pytest.raises(CommandError, match="cannot be enabled in availability-b"):
        call_command("set_app_availability", NOTES, "--mode", "all", "--enable", "availability-b", stdout=StringIO())
    with system_context(reason="test:cli-refused", organization_id=org_b.pk):
        assert not AppInstallation.objects.filter(app_label=NOTES).exists()
