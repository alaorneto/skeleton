"""First-party apps shipped inside the core wheel (#66): manifest, location, version, opt-in."""

import ast
import json
from copy import deepcopy
from pathlib import Path

import pytest
from django.apps import apps
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from skeleton.conf.bundled import bundled_app_configs
from skeleton.system import app_registry
from skeleton.system.packages import PackageError, validate_manifest
from skeleton.versioning import core_version

ROOT = Path(__file__).resolve().parents[2]


def bundled_manifest(**changes):
    manifest = json.loads((ROOT / "examples" / "notes" / "manifest.json").read_text(encoding="utf-8"))
    manifest.update(
        kind="bundled_app",
        app_id="org.skeleton.agenda-test",
        app_label="skeleton_agenda",
        package_name="agenda",
        app_config="skeleton.contrib.agenda.apps.AgendaConfig",
        version=core_version()["version"] or "0.4.0",
        migrations={"leaves": ["0001_initial"], "from_versions": ">=0.0.0", "labels": ["skeleton_agenda"]},
    )
    runtime = manifest["runtime"]
    runtime.update(
        urls="skeleton.contrib.agenda.urls",
        api_urls="skeleton.contrib.agenda.api_urls",
        user_urls="skeleton.contrib.agenda.user_urls",
        service_api={"module": "skeleton.contrib.agenda.service_api", "classes": ["AgendaService"]},
        navigation=[],
        model_ui=[],
    )
    manifest.update(changes)
    return manifest


def test_a_valid_bundled_manifest_is_accepted():
    validated = validate_manifest(bundled_manifest())
    assert validated["kind"] == "bundled_app" and validated["runtime"]["user_urls"].startswith("skeleton.contrib.")


@pytest.mark.parametrize(
    "change",
    [
        {"app_config": "applications.agenda.apps.AgendaConfig"},  # Outside skeleton.contrib.
        {"app_label": "agenda", "migrations": {"leaves": [], "from_versions": ">=0.0.0", "labels": ["agenda"]}},
        {"manifest_version": 1},
        {"files": [{"path": "app/x.py", "size": 1, "sha256": "0" * 64, "role": "code"}]},
    ],
    ids=["outside-package", "without-prefix", "manifest-v1", "own-files"],
)
def test_invalid_bundled_manifests_are_refused(change):
    with pytest.raises(PackageError):
        validate_manifest(bundled_manifest(**change))


def test_third_party_apps_cannot_use_the_reserved_prefix_or_user_routes():
    third = json.loads((ROOT / "examples" / "notes" / "manifest.json").read_text(encoding="utf-8"))
    reserved = deepcopy(third)
    reserved.update(app_label="skeleton_notes")
    reserved["migrations"] = {**reserved["migrations"], "labels": ["skeleton_notes"]}
    with pytest.raises(PackageError, match="reserved"):
        validate_manifest(reserved)
    user_routes = deepcopy(third)
    user_routes["runtime"]["user_urls"] = "applications.notes.user_urls"
    with pytest.raises(PackageError):
        validate_manifest(user_routes)


def write_manifest(directory, manifest):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def config(directory, name="skeleton.contrib.agenda"):
    return type("AgendaConfig", (), {"label": "skeleton_agenda", "name": name, "path": str(directory)})()


def test_bundled_manifest_outside_the_core_package_is_refused(tmp_path):
    directory = tmp_path / "agenda"
    path = write_manifest(directory, bundled_manifest())
    with pytest.raises(PackageError, match="inside the installed core"):
        app_registry.AppManifest.from_json(path, app_config=config(directory))


def test_divergent_version_stops_the_startup(tmp_path, monkeypatch):
    monkeypatch.setattr(app_registry, "core_package_root", lambda: tmp_path)
    directory = tmp_path / "contrib" / "agenda"
    path = write_manifest(directory, bundled_manifest(version="9.9.9"))
    with pytest.raises(ImproperlyConfigured, match="differs from the core"):
        app_registry.AppManifest.from_json(path, app_config=config(directory))
    with pytest.raises(PackageError, match="inside the installed core"):
        app_registry.AppManifest.from_json(path, app_config=config(directory, name="skeleton.contrib.other"))


def test_contrib_namespace_is_reserved_to_bundled_manifests(tmp_path):
    third = json.loads((ROOT / "examples" / "notes" / "manifest.json").read_text(encoding="utf-8"))
    path = write_manifest(tmp_path / "notes", third)
    impostor = type(
        "NotesConfig", (), {"label": "example_notes", "name": "skeleton.contrib.notes", "path": str(tmp_path / "notes")}
    )()
    with pytest.raises(PackageError):
        app_registry.AppManifest.from_json(path, app_config=impostor)


def test_opt_in_comes_from_the_fixed_map_only():
    assert bundled_app_configs([]) == []
    with pytest.raises(ImproperlyConfigured, match="Unknown first-party app"):
        bundled_app_configs(["skeleton.contrib.agenda.apps.AgendaConfig"])  # Import paths are not names.
    available = {"agenda": "skeleton.contrib.agenda.apps.AgendaConfig"}
    assert bundled_app_configs([" agenda ", "agenda", ""], available=available) == [available["agenda"]]


def test_the_suite_runs_without_any_bundled_app():
    assert settings.SKELETON_BUNDLED_APPS == ()
    assert not [config for config in apps.get_app_configs() if config.name.startswith("skeleton.contrib.")]


def test_the_core_never_imports_contrib():
    offenders = []
    for path in (ROOT / "skeleton").rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith("skeleton/contrib/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            elif isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) in (
                "import_module",
                "__import__",
            ):
                names = [arg.value for arg in node.args if isinstance(arg, ast.Constant) and isinstance(arg.value, str)]
            offenders += [f"{relative}: {name}" for name in names if name.startswith("skeleton.contrib")]
    assert offenders == []
