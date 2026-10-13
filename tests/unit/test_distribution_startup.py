"""Startup metadata and maintenance must never require a database connection."""
import json
from pathlib import Path

import pytest
from django.apps.registry import Apps
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpResponse
from django.test import RequestFactory, override_settings

from skeleton.deployment import (DeploymentInventory, MaintenanceMiddleware, load_deployment_inventory,
                                 migration_archive_configs, release_response)
from skeleton.system.packages import manifest_template


def manifest(label="sample_notes", package="sample_notes"):
    return manifest_template(app_id=label, app_label=label, package_name=package, publisher="test-publisher")


def test_private_inventory_runtime_excludes_retained_and_preserves_hashes(tmp_path):
    current, removed = manifest(), manifest("removed_notes", "removed_notes")
    data = {"generation": 7, "core": {}, "apps": [
        {"manifest": current, "digest": "a" * 64, "state": "installed", "path": "applications/sample_notes"},
        {"manifest": removed, "digest": "b" * 64, "state": "schema_retained", "path": "archives/archive_removed_notes"}],
        "retained_schema_paths": [{"path": "retained/removed.json", "sha256": "c" * 64}],
        "migration_archives": [{"app_label": "removed_notes", "package_name": "removed_notes",
                                "path": "archives/archive_removed_notes", "migrations_module": "archive_removed_notes.migrations"}]}
    inventory_file = tmp_path / "inventory.json"
    inventory_file.write_text(json.dumps(data))
    state = load_deployment_inventory(inventory_file)
    assert state.generation == 7
    assert state.installed_apps == (current["app_config"],)
    assert state.retained_schema_paths == ({"path": str(tmp_path / "retained/removed.json"), "sha256": "c" * 64},)
    assert len(state.migration_archives) == 1
    data["apps"].append(data["apps"][0])
    inventory_file.write_text(json.dumps(data))
    with pytest.raises(ImproperlyConfigured):
        load_deployment_inventory(inventory_file)


def test_migrator_loads_genuine_archive_with_original_label_no_models_or_ready(tmp_path, monkeypatch):
    archive = tmp_path / "archive_removed_notes"
    (archive / "migrations").mkdir(parents=True)
    (archive / "__init__.py").write_text("")
    (archive / "migrations/__init__.py").write_text("")
    (archive / "migrations/0001_initial.py").write_text(
        "from django.db import migrations\nclass Migration(migrations.Migration):\n initial=True\n dependencies=[]\n operations=[]\n")
    item = {"app_label": "removed_notes", "package_name": "removed_notes", "path": str(archive),
            "migrations_module": "archive_removed_notes.migrations"}
    state = DeploymentInventory(1, {}, (), (), (item,))
    monkeypatch.syspath_prepend(str(tmp_path))
    configs, modules = migration_archive_configs(state)
    registry = Apps(configs)
    config = registry.get_app_config("removed_notes")
    assert config.models_module is None
    assert list(registry.get_models()) == []
    from django.db.migrations.loader import MigrationLoader
    with override_settings(INSTALLED_APPS=configs, MIGRATION_MODULES=modules):
        loader = MigrationLoader(None)
    assert ("removed_notes", "0001_initial") in loader.graph.nodes
    (archive / "apps.py").write_text("raise RuntimeError('business hooks must not load')")
    with pytest.raises(ImproperlyConfigured, match="Unsafe"):
        migration_archive_configs(state)


def test_maintenance_stops_all_application_requests_before_session_or_db(tmp_path):
    marker = tmp_path / "maintenance"
    marker.touch()
    called = []
    state = DeploymentInventory(0, {}, (), (), ())
    with override_settings(SKELETON_MAINTENANCE_MARKER=str(marker), SKELETON_DEPLOYMENT_STATE=state):
        middleware = MaintenanceMiddleware(lambda request: called.append(request) or HttpResponse("active"))
        response = middleware(RequestFactory().post("/admin/system/releases/"))
        assert response.status_code == 503
        assert response["Cache-Control"] == "no-store"
        assert called == []
        response = middleware(RequestFactory().get("/health/release/"))
        assert response.status_code == 200
        assert json.loads(response.content)["generation"] == 0
        assert called == []
        marker.unlink()
        assert middleware(RequestFactory().get("/profile/")).status_code == 200
        assert len(called) == 1


def test_release_confirmation_refuses_unmatched_build_and_requires_safe_methods():
    state = DeploymentInventory(9, {"version": "0.1.0", "digest": "a" * 64}, (), (), ())
    with override_settings(SKELETON_DEPLOYMENT_STATE=state):
        response = release_response(RequestFactory().get("/health/release/"), ({"version": "0.1.0", "origin": "installed"}, False))
        assert response.status_code == 503
        assert json.loads(response.content)["status"] == "release_mismatch"
        assert release_response(RequestFactory().post("/health/release/")).status_code == 405
        response = release_response(RequestFactory().head("/health/release/"), ({"version": "0.1.0", "origin": "installed"}, True))
        assert response.status_code == 200
        assert response.content == b""


@pytest.mark.parametrize('mismatch', ['version', 'path'])
def test_local_release_inventory_cannot_disagree_with_loaded_app(mismatch, tmp_path):
    from importlib import import_module
    from examples.notes.apps import NotesConfig
    from skeleton.system.app_registry import AppManifest
    from skeleton.system.packages import PackageError, load_manifest
    config = NotesConfig('examples.notes', import_module('examples.notes'))
    source = Path(config.path) / 'manifest.json'
    declared = load_manifest(source)
    entry = {'manifest': declared, 'state': 'project-local', 'path': config.path}
    if mismatch == 'version':
        entry['manifest'] = {**declared, 'version': '9.9.9'}
    else:
        entry['path'] = str(tmp_path / 'different-app')
    inventory = DeploymentInventory(0, {}, (entry,), (), ())
    with override_settings(SKELETON_DEPLOYMENT_STATE=inventory):
        with pytest.raises(PackageError):
            AppManifest.from_json(source, app_config=config)
