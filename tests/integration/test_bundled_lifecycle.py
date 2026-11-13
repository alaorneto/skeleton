"""First-party apps in the deployment state (#67): closed on install, retained on opt-out, kept on opt-in.

The core has no first-party app yet, so a manifest flagged ``bundled`` stands in for one
(registered only in this test); the table-level retention is proven with the calendar (#84).
"""

from dataclasses import replace

import pytest
from django.core.exceptions import ImproperlyConfigured

from skeleton.conf import bundled as bundled_conf
from skeleton.system import app_registry
from skeleton.system.bundled import bundled_divergences, sync_bundled_components
from skeleton.system.models import AppAvailability, AppInstallation, ComponentInstallation
from skeleton.tenancy.context import system_context
from tools.security_fixtures import build_security_fixture

pytestmark = pytest.mark.usefixtures("skeleton_db")
LABEL = "example_notes"


@pytest.fixture
def stand_in(monkeypatch):
    """Example notes recorded as if it were a first-party app called 'notes'."""
    manifest = replace(app_registry.get_app(LABEL), bundled=True, app_id="org.skeleton.bundled-stand-in")
    monkeypatch.setitem(app_registry._manifests, LABEL, manifest)
    monkeypatch.setattr(bundled_conf, "BUNDLED_LABELS", {"notes": LABEL})
    monkeypatch.setattr(bundled_conf, "BUNDLED_SNAPSHOTS", {"notes": "contrib/notes/retained_schema.json"})
    return manifest


def component():
    with system_context(reason="test:bundled"):
        return ComponentInstallation.all_objects.get(component_id="org.skeleton.bundled-stand-in")


@pytest.mark.needs_example_app
def test_a_new_first_party_app_starts_unavailable_and_inactive(stand_in, monkeypatch):
    access = build_security_fixture()
    fresh = replace(
        stand_in, app_label="skeleton_standin", app_id="org.skeleton.fresh-stand-in", package_name="standin"
    )
    monkeypatch.setitem(app_registry._manifests, "skeleton_standin", fresh)
    report = sync_bundled_components()
    assert "skeleton_standin" in report["installed"]
    with system_context(reason="test:bundled"):
        row = ComponentInstallation.all_objects.get(component_id="org.skeleton.fresh-stand-in")
        availability = AppAvailability.all_objects.get(app_label="skeleton_standin")
    assert (row.kind, row.state, row.version) == ("bundled", "bundled", fresh.version)
    assert availability.mode == AppAvailability.Mode.SELECTED  # No grant: unavailable everywhere.
    for organization in (access.a, access.b):
        with system_context(reason="test:bundled-grants", organization_id=organization.pk):
            assert not AppInstallation.objects.filter(app_label="skeleton_standin").exists()
    assert sync_bundled_components()["installed"] == []  # Idempotent.
    assert bundled_divergences() == []


@pytest.mark.needs_example_app
def test_opt_out_retains_and_deactivates_and_opt_in_keeps_the_rows(stand_in, monkeypatch, settings):
    access = build_security_fixture()
    sync_bundled_components()
    from skeleton.testing.applications import activate_app, open_app

    open_app(LABEL)
    activate_app(LABEL, access.a.pk)
    with system_context(reason="test:bundled", organization_id=access.a.pk):
        installation = AppInstallation.objects.get(app_label=LABEL)
        configuration, sysid = dict(installation.configuration), installation.pk
    # Opted out without declaring the retention: the schema check reports it.
    monkeypatch.setitem(app_registry._manifests, LABEL, replace(stand_in, bundled=False))
    assert any("without SKELETON_BUNDLED_RETAINED" in problem for problem in bundled_divergences())
    settings.SKELETON_BUNDLED_RETAINED = ("notes",)
    report = sync_bundled_components()
    assert report["retained"] == [LABEL]
    assert component().state == "schema_retained"
    with system_context(reason="test:bundled", organization_id=access.a.pk):
        assert AppInstallation.objects.get(pk=sysid).enabled is False
    assert bundled_divergences() == []
    # Opted in again: the same rows, the same configuration, still inactive until the organization acts.
    monkeypatch.setitem(app_registry._manifests, LABEL, stand_in)
    assert any("opted in again" in problem for problem in bundled_divergences())
    settings.SKELETON_BUNDLED_RETAINED = ()
    assert sync_bundled_components()["reinstalled"] == [LABEL]
    assert component().state == "bundled"
    with system_context(reason="test:bundled", organization_id=access.a.pk):
        again = AppInstallation.objects.get(pk=sysid)
    assert again.configuration == configuration and again.enabled is False


@pytest.mark.needs_example_app
def test_opted_in_but_unrecorded_is_a_divergence(stand_in):
    assert any("not recorded" in problem for problem in bundled_divergences())


def test_retained_names_come_from_the_fixed_map():
    assert bundled_conf.retained_snapshot_paths([]) == []
    with pytest.raises(ImproperlyConfigured, match="Unknown first-party app"):
        bundled_conf.retained_snapshot_paths(["calendar"], snapshots={})
    with pytest.raises(ImproperlyConfigured, match="installed and retained"):
        bundled_conf.retained_snapshot_paths(["notes"], installed=("notes",), snapshots={"notes": "x.json"})
    [path] = bundled_conf.retained_snapshot_paths(["notes"], snapshots={"notes": "contrib/notes/retained.json"})
    assert path.replace("\\", "/").endswith("skeleton/contrib/notes/retained.json")


@pytest.mark.needs_example_app
def test_retention_publishes_app_disabled_per_active_organization(stand_in, monkeypatch, settings):
    """Retiring an app with its data publishes app.disabled where it was on, and nowhere else (#282)."""
    from skeleton.events.models import DomainEvent

    access = build_security_fixture()
    sync_bundled_components()
    from skeleton.testing.applications import activate_app, open_app

    open_app(LABEL)
    activate_app(LABEL, access.a.pk)
    with system_context(reason="test:bundled", organization_id=access.a.pk):
        configuration = dict(AppInstallation.objects.get(app_label=LABEL).configuration)
    with system_context(reason="test:bundled-b-off", organization_id=access.b.pk):
        for row in AppInstallation.objects.filter(app_label=LABEL, enabled=True):  # B has the app off.
            row.enabled = False
            row.save(update_fields={"enabled"})
    monkeypatch.setitem(app_registry._manifests, LABEL, replace(stand_in, bundled=False))
    settings.SKELETON_BUNDLED_RETAINED = ("notes",)
    sync_bundled_components()

    def disabled(organization):
        with system_context(reason="test:bundled-events", organization_id=organization.pk):
            return list(DomainEvent.objects.filter(event_type="app.disabled").values_list("data", flat=True))

    assert disabled(access.a) == [{"app_label": LABEL}]
    assert disabled(access.b) == []
    with system_context(reason="test:bundled", organization_id=access.a.pk):
        assert dict(AppInstallation.objects.get(app_label=LABEL).configuration) == configuration
