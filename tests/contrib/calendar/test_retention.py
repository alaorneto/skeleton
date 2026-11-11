"""Retiring and reinstalling the calendar (#67): tables kept and checked by the wheel snapshot, IDs kept."""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from skeleton.conf.bundled import retained_snapshot_paths
from skeleton.contrib.calendar import services
from skeleton.contrib.calendar.models import Calendar, Event
from skeleton.system import app_registry
from skeleton.system.bundled import bundled_divergences, sync_bundled_components
from skeleton.system.infrastructure_snapshot import export_inventory
from skeleton.system.models import AppInstallation, ComponentInstallation
from skeleton.system.retained_schema import export_schema
from skeleton.tenancy.context import system_context
from tools.security_fixtures import actor_tenant

LABEL = "skeleton_calendar"
START = datetime(2027, 2, 1, 9, 0, tzinfo=UTC)


def test_the_wheel_snapshot_matches_the_installed_models(agenda):
    [path] = retained_snapshot_paths(["calendar"])
    shipped = json.loads(Path(path).read_text(encoding="utf-8"))
    assert shipped["tables"] == export_schema(LABEL)["tables"], (
        "Regenerate retained_schema.json (export_record_schema)."
    )


def without_calendar_specs(monkeypatch):
    from skeleton.system import checks, synchronization
    from skeleton.system.registry import all_specs

    installed = tuple(spec for spec in all_specs() if spec.model._meta.app_label != LABEL)
    monkeypatch.setattr(checks, "all_specs", lambda: installed)
    monkeypatch.setattr(synchronization, "all_specs", lambda: installed)


def frozen_inventory(tmp_path):
    frozen = tmp_path / "infrastructure.json"
    raw = json.dumps(export_inventory(), sort_keys=True).encode()
    frozen.write_bytes(raw)
    return {"path": str(frozen), "sha256": hashlib.sha256(raw).hexdigest()}


def test_opt_out_keeps_tables_checked_and_opt_in_keeps_ids(agenda, monkeypatch, settings, tmp_path):
    with actor_tenant(agenda.alice, agenda.a):
        calendar = services.ensure_personal_calendar(agenda.alice)
        event = services.create_event(
            actor=agenda.alice,
            calendar_id=calendar.pk,
            data={"title": "Preservado", "starts_at": START, "ends_at": START + timedelta(hours=1)},
        )
    sync_bundled_components()
    frozen = frozen_inventory(tmp_path)
    # Opt out: the app's code is gone (specs and manifest), its tables remain.
    without_calendar_specs(monkeypatch)
    manifest = app_registry.get_app(LABEL)
    monkeypatch.setitem(app_registry._manifests, LABEL, replace(manifest, bundled=False))
    with override_settings(SKELETON_FROZEN_INFRASTRUCTURE=frozen):
        with pytest.raises(CommandError):  # Not declared retained: unclassified tables and a divergence.
            call_command("check_skeleton_schema", verbosity=0)
        assert any("without SKELETON_BUNDLED_RETAINED" in problem for problem in bundled_divergences())
        settings.SKELETON_BUNDLED_RETAINED = ("calendar",)
        retained = [
            {"path": path, "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()}
            for path in retained_snapshot_paths(["calendar"])
        ]
        with override_settings(SKELETON_RETAINED_SCHEMA_PATHS=retained):
            assert sync_bundled_components()["retained"] == [LABEL]
            call_command("sync_table_catalog", verbosity=0)
            call_command("check_skeleton_schema", verbosity=0)  # Tables, guards and RLS still exact.
    with system_context(reason="test:retention"):
        assert ComponentInstallation.all_objects.get(app_label=LABEL).state == "schema_retained"
    with system_context(reason="test:retention", organization_id=agenda.a.pk):
        assert AppInstallation.all_objects.get(app_label=LABEL).enabled is False
        assert Event.all_objects.filter(pk=event.pk).exists()  # Data preserved.
    # Opt in again: same rows and UUIDs, inactive until the organization activates it.
    monkeypatch.undo()
    settings.SKELETON_BUNDLED_RETAINED = ()
    assert sync_bundled_components()["reinstalled"] == [LABEL]
    with system_context(reason="test:retention", organization_id=agenda.a.pk):
        assert Calendar.all_objects.get(pk=calendar.pk).owner_id == agenda.alice.pk
        assert Event.all_objects.get(pk=event.pk).title == "Preservado"
        assert AppInstallation.all_objects.get(app_label=LABEL).enabled is False
