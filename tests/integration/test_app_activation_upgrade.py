"""Installed apps are unavailable and disabled by default; the upgrade keeps what was in use (#22)."""

from dataclasses import replace
from io import StringIO

import pytest
from django.core.management import call_command
from django.test import Client
from django.urls import reverse

from skeleton.accounts.services import bootstrap_identity, create_organization
from skeleton.system import activation_upgrade, app_registry
from skeleton.system.activation_upgrade import upgrade_app_activation
from skeleton.system.app_registry import AppManifest, register_app, unregister_app
from skeleton.system.applications import application_state
from skeleton.tenancy.context import system_context, tenant_context

# These tests prove the closed default: the skeleton_db fixture must not open any app.
pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.closed_app_defaults]
PASSWORD = "Closed-default-Password!22"
NOTES = "example_notes"
QUIET = "quiet_fixture"


def require_notes():
    from django.apps import apps

    if not apps.is_installed("examples.notes"):
        pytest.skip("SKELETON_ENABLE_EXAMPLE_APP=true is required")


@pytest.fixture
def legacy(monkeypatch):
    """A deployment before #22: Notes active by default, a second app off by default, no rows."""
    require_notes()
    monkeypatch.setitem(app_registry._manifests, NOTES, replace(app_registry.get_app(NOTES), default_enabled=True))
    register_app(AppManifest(app_label=QUIET, label="Quiet", description="Fixture", default_enabled=False))
    admin, org_a = bootstrap_identity(
        email="upgrade-admin@example.test", organization="upgrade-a", name="A", password=PASSWORD
    )
    _, org_b = bootstrap_identity(email=admin.email, organization="upgrade-b", name="B")
    from skeleton.system.models import AppInstallation

    # B had explicitly disabled Notes before the upgrade.
    with system_context(reason="test:legacy-choice", organization_id=org_b.pk):
        AppInstallation(organization=org_b, app_label=NOTES, enabled=False, configuration={"heading": "B"}).save()
    yield admin, org_a, org_b
    unregister_app(QUIET)


def state(admin, organization, label):
    with tenant_context(organization_id=organization.pk, actor=admin, source="test:upgrade-state"):
        current = application_state(label)
        return current["available"], current["enabled"]


def installations(organization):
    from skeleton.system.models import AppInstallation

    with system_context(reason="test:upgrade-rows", organization_id=organization.pk):
        return sorted(AppInstallation.objects.values_list("app_label", "enabled"))


def availability_rows():
    from skeleton.system.models import AppAvailability

    with system_context(reason="test:upgrade-availability"):
        return {row.app_label: (row.mode, row.upgrade_state) for row in AppAvailability.objects.all()}


def test_without_the_upgrade_no_app_is_reachable(legacy):
    admin, org_a, org_b = legacy
    assert state(admin, org_a, NOTES) == (False, False)
    assert state(admin, org_b, NOTES) == (False, False)
    client = Client()
    assert client.post(reverse("account_login"), {"login": admin.email, "password": PASSWORD}).status_code == 302
    assert client.get(reverse("example_notes:home", kwargs={"organization_id": org_a.pk})).status_code == 404
    records = {"organization_id": org_a.pk, "table_key": "example_notes.note"}
    assert client.get(reverse("api:records", kwargs=records)).status_code == 404


def test_upgrade_keeps_exactly_the_apps_that_were_active(legacy):
    from skeleton.audit.models import AuditEvent
    from skeleton.system.models import AppInstallation

    admin, org_a, org_b = legacy
    report = upgrade_app_activation()
    assert sorted(report.created_availability) == [NOTES, QUIET]
    assert report.activations == {org_a.pk: [NOTES]}
    assert availability_rows() == {NOTES: ("all", "done"), QUIET: ("all", "done")}
    assert state(admin, org_a, NOTES) == (True, True)
    assert state(admin, org_b, NOTES) == (True, False)
    assert state(admin, org_a, QUIET) == (True, False)
    assert installations(org_a) == [(NOTES, True)]
    assert installations(org_b) == [(NOTES, False)]
    with system_context(reason="test:upgrade-audit", organization_id=org_a.pk):
        record = AppInstallation.objects.get(app_label=NOTES)
        event = AuditEvent.objects.get(record_sysid=record.pk, operation="create")
        assert event.source == "system:upgrade:app-activation" and event.organization_id == org_a.pk

    later = create_organization(owner=admin, slug="upgrade-later", name="Later")
    again = upgrade_app_activation()
    assert again.created_availability == [] and again.activations == {} and again.completed == []
    assert installations(later) == []
    assert state(admin, later, NOTES) == (True, False)


def test_app_installed_after_the_upgrade_is_born_selected(legacy):
    from skeleton.system.models import AppOrganizationGrant
    from skeleton.system.releases import synchronize_inventory

    admin, org_a, _ = legacy
    upgrade_app_activation()
    register_app(AppManifest(app_label="fresh_fixture", label="Fresh", description="Fixture", default_enabled=True))
    try:
        inventory = {
            "generation": 2,
            "apps": [
                {
                    "manifest": {"app_id": "org.test.fresh", "app_label": "fresh_fixture", "version": "0.1.0"},
                    "state": "installed",
                    "digest": "f" * 64,
                }
            ],
        }
        with system_context(reason="deployment:reconcile"):
            synchronize_inventory(inventory)
        assert availability_rows()["fresh_fixture"] == ("selected", "done")
        with system_context(reason="test:fresh-grants"):
            assert not AppOrganizationGrant.objects.filter(availability__app_label="fresh_fixture").exists()
        # The upgrade now trusts the inventory: the new app is not legacy and stays closed.
        assert upgrade_app_activation().created_availability == []
        assert state(admin, org_a, "fresh_fixture") == (False, False)
    finally:
        unregister_app("fresh_fixture")


def test_agent_inventory_decides_what_was_installed_before(legacy):
    from skeleton.system.models import ComponentInstallation

    admin, org_a, _ = legacy
    with system_context(reason="test:known-components"):
        ComponentInstallation(
            component_id="org.skeleton.example-notes", kind="app", app_label=NOTES, version="0.3.0", state="installed"
        ).save()
    # QUIET is loaded by the new release but unknown to the previous inventory: a new app.
    report = upgrade_app_activation()
    assert report.created_availability == [NOTES]
    assert QUIET not in availability_rows()
    assert state(admin, org_a, NOTES) == (True, True)


@pytest.mark.parametrize("stop_after", ["prepare", "activate"])
def test_interrupted_upgrade_resumes_without_duplicates(legacy, stop_after):
    admin, org_a, org_b = legacy
    upgrade_app_activation(stop_after=stop_after)
    assert availability_rows()[NOTES] == ("all", "pending")
    report = upgrade_app_activation()
    assert report.completed == [NOTES, QUIET]
    assert availability_rows() == {NOTES: ("all", "done"), QUIET: ("all", "done")}
    assert installations(org_a) == [(NOTES, True)]
    assert installations(org_b) == [(NOTES, False)]


def test_failure_in_one_organization_keeps_the_others_and_resumes(legacy, monkeypatch):
    admin, org_a, _ = legacy
    third = create_organization(owner=admin, slug="upgrade-c", name="C")
    calls = []
    original = app_registry.validate_configuration

    def fail_once(manifest, values):
        if values == {}:  # The upgrade's own call; model validation passes the stored defaults.
            calls.append(manifest.app_label)
        if len(calls) == 2:
            raise RuntimeError("simulated crash")
        return original(manifest, values)

    monkeypatch.setattr(app_registry, "validate_configuration", fail_once)
    with pytest.raises(RuntimeError):
        upgrade_app_activation()
    done = [org for org in (org_a, third) if installations(org)]
    assert len(done) == 1  # The first organization committed; the crashed one rolled back alone.
    assert availability_rows()[NOTES] == ("all", "pending")
    monkeypatch.setattr(app_registry, "validate_configuration", original)
    upgrade_app_activation()
    assert installations(org_a) == [(NOTES, True)] and installations(third) == [(NOTES, True)]
    assert availability_rows()[NOTES] == ("all", "done")


def test_dry_run_lists_and_writes_nothing(legacy):
    _, org_a, _ = legacy
    output = StringIO()
    call_command("upgrade_app_activation", "--dry-run", stdout=output)
    text = output.getvalue()
    assert f"Would create availability all for {NOTES}." in text
    assert f"Would create activation in organization {org_a.pk}: {NOTES}." in text
    assert availability_rows() == {}
    assert installations(org_a) == []


def test_rows_written_before_the_upgrade_are_upgraded_once(legacy):
    from skeleton.system.models import AppAvailability, AppOrganizationGrant

    admin, org_a, org_b = legacy
    with system_context(reason="test:pre-22-selected"):
        row = AppAvailability(app_label=NOTES, mode="selected")  # #21 wrote rows with upgrade_state "".
        row.save()
        AppOrganizationGrant(availability=row, granted_organization=org_a).save()
    report = upgrade_app_activation()
    assert report.marked_pending == [NOTES] and report.created_availability == [QUIET]
    assert availability_rows()[NOTES] == ("selected", "done")
    assert state(admin, org_a, NOTES) == (True, True)
    assert state(admin, org_b, NOTES) == (False, False)


def test_cli_opens_an_app_for_development_and_check_warns_while_closed(legacy):
    admin, org_a, org_b = legacy
    warnings = StringIO()
    call_command("check_skeleton_schema", stderr=warnings, stdout=StringIO())
    assert f"installed app {NOTES} has no availability row" in warnings.getvalue()
    call_command("set_app_availability", NOTES, "--mode", "selected", "--enable", "upgrade-a", stdout=StringIO())
    assert state(admin, org_a, NOTES) == (True, True)
    assert state(admin, org_b, NOTES) == (False, False)
    warnings = StringIO()
    call_command("check_skeleton_schema", stderr=warnings, stdout=StringIO())
    assert NOTES not in warnings.getvalue()


def test_apps_without_availability_lists_loaded_apps_only(legacy):
    with system_context(reason="test:closed-list"):
        assert {NOTES, QUIET} <= set(activation_upgrade.apps_without_availability())
