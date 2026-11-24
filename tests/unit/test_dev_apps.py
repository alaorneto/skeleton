"""SKELETON_DEV_APPS (#518): apps loaded from their own checkout, in development and test only."""

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.core.exceptions import ImproperlyConfigured

from skeleton.conf import profiles
from skeleton.conf.dev_apps import REFUSED, DevAppFinder, dev_app_configs
from skeleton.system.app_scaffold import build_scaffold, create_application

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _isolated_imports():
    meta_path, path, modules = list(sys.meta_path), list(sys.path), set(sys.modules)
    yield
    sys.meta_path[:] = meta_path
    sys.path[:] = path
    for name in set(sys.modules) - modules:
        if name.startswith("applications."):
            del sys.modules[name]


def checkout(tmp_path, label="acme_devapp", **changes):
    scaffold = build_scaffold(app_label=label, app_id=f"com.example.{label}", publisher="example-publisher")
    root, _ = create_application(scaffold, tmp_path)
    if changes:
        manifest_path = root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.update(changes)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return root


def test_a_checkout_is_imported_under_its_managed_namespace(tmp_path):
    root = checkout(tmp_path)
    path_before = list(sys.path)

    configs = dev_app_configs([str(root), f"  {root}  ", ""])

    assert configs == ["applications.acme_devapp.apps.AcmeDevappConfig"]
    module = importlib.import_module("applications.acme_devapp")
    assert Path(module.__file__).resolve() == (root / "__init__.py").resolve()
    assert sys.path == path_before  # nothing is added: siblings never become importable
    assert importlib.util.find_spec(f"applications.{tmp_path.name}") is None


def test_the_order_is_kept_without_duplicates(tmp_path):
    first, second = checkout(tmp_path / "a", "acme_first"), checkout(tmp_path / "b", "acme_second")
    assert dev_app_configs([second, first, second]) == [
        "applications.acme_second.apps.AcmeSecondConfig",
        "applications.acme_first.apps.AcmeFirstConfig",
    ]


def test_the_finder_provides_the_namespace_only_when_no_project_has_one():
    spec = DevAppFinder().find_spec("applications")
    assert spec.submodule_search_locations == [] and spec.loader is None
    assert DevAppFinder().find_spec("applications.unknown") is None
    assert DevAppFinder().find_spec("other.module") is None


def refusal(paths):
    with pytest.raises(ImproperlyConfigured) as error:
        dev_app_configs(paths)
    return str(error.value)


def test_a_missing_directory_is_refused(tmp_path):
    message = refusal([str(tmp_path / "missing")])
    assert str(tmp_path / "missing") in message and "não existe" in message


def test_a_directory_without_manifest_is_refused(tmp_path):
    (tmp_path / "acme_empty").mkdir()
    message = refusal([str(tmp_path / "acme_empty")])
    assert "acme_empty" in message and "manifest.json" in message


def test_a_manifest_that_is_not_json_is_refused(tmp_path):
    root = tmp_path / "acme_broken"
    root.mkdir()
    (root / "manifest.json").write_text("{", encoding="utf-8")
    assert "JSON" in refusal([root])


@pytest.mark.parametrize("kind", ["core", "bundled_app", None])
def test_a_manifest_that_is_not_an_app_is_refused(tmp_path, kind):
    root = checkout(tmp_path, kind=kind)
    message = refusal([root])
    assert str(root) in message and 'kind "app"' in message


@pytest.mark.parametrize(
    "changes",
    [
        {"app_label": "skeleton_devapp"},
        {"app_label": "sk_devapp"},
        {"app_config": "skeleton.contrib.calendar.apps.CalendarConfig"},
    ],
)
def test_reserved_labels_and_core_app_configs_are_refused(tmp_path, changes):
    root = checkout(tmp_path, **changes)
    message = refusal([root])
    assert str(root) in message and "reservado" in message


def test_an_invalid_manifest_is_refused_with_the_validator_message(tmp_path):
    root = checkout(tmp_path, app_config="applications.other.apps.OtherConfig")
    message = refusal([root])
    assert str(root) in message and "manifesto inválido" in message


def test_the_directory_must_be_named_after_the_package(tmp_path):
    root = checkout(tmp_path)
    renamed = root.rename(tmp_path / "clone")
    message = refusal([renamed])
    assert str(renamed) in message and "'acme_devapp'" in message


def test_a_package_already_importable_elsewhere_is_refused(tmp_path):
    import applications

    shadow = tmp_path / "shadow"
    (shadow / "acme_devapp").mkdir(parents=True)
    (shadow / "acme_devapp" / "__init__.py").write_text("", encoding="utf-8")
    applications.__path__.append(str(shadow))
    try:
        message = refusal([checkout(tmp_path / "repo")])
    finally:
        applications.__path__.remove(str(shadow))
    assert "já é importável" in message


def test_the_same_package_from_two_checkouts_is_refused(tmp_path):
    dev_app_configs([checkout(tmp_path / "a")])
    assert "já foi carregado" in refusal([checkout(tmp_path / "b")])


def base_with(value):
    import skeleton.conf.base as base

    return {**vars(base), "SKELETON_DEV_APPS": value}


@pytest.mark.parametrize(
    "profile",
    [profiles.production_settings, profiles.migrate_settings, profiles.build_settings, profiles.setup_settings],
)
def test_every_profile_but_development_and_test_refuses_the_variable(profile):
    with pytest.raises(ImproperlyConfigured) as error:
        profile(base_with(("/srv/acme",)))
    assert str(error.value) == REFUSED


def test_development_and_test_profiles_accept_the_variable():
    assert profiles.development_settings(base_with(("/srv/acme",)))["DEBUG"] is True
    assert profiles.test_settings(base_with(("/srv/acme",)))["DEBUG"] is True


def test_an_empty_variable_changes_nothing():
    profiles.build_settings(base_with(()))


def test_the_host_project_runs_without_the_config_package(tmp_path):
    script = (
        "import sys\n"
        "class Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'config' or name.startswith('config.'):\n"
        "            raise ImportError('config must not be imported')\n"
        "sys.meta_path.insert(0, Block())\n"
        "import django; django.setup()\n"
        "from django.urls import reverse\n"
        "from django.conf import settings\n"
        "print(settings.ROOT_URLCONF, reverse('account_login'), 'config' in sys.modules)\n"
    )
    environment = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "skeleton.testing.host.settings",
        "SKELETON_PROJECT_ROOT": str(tmp_path),
        "PYTHONPATH": str(ROOT),
    }
    environment.pop("SKELETON_DEV_APPS", None)
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr[-3000:]
    assert result.stdout.split()[-3:] == ["skeleton.testing.host.urls", "/accounts/login/", "False"]


def test_a_dev_app_installs_the_library_it_depends_on(tmp_path):
    """#793: the ledger enters INSTALLED_APPS for the dev app that depends on it, without the opt-in."""
    root = checkout(tmp_path, label="acme_ledger_user")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest["dependencies"] = {"org.skeleton.ledger": ">=0.0.0"}
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    script = (
        "import django; django.setup()\n"
        "from django.apps import apps\n"
        "from django.conf import settings\n"
        "print(apps.is_installed('skeleton.contrib.ledger'), apps.is_installed('skeleton.contrib.calendar'),"
        " ','.join(settings.SKELETON_BUNDLED_INSTALLED))\n"
    )
    environment = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "config.settings.test",
        "SKELETON_DEV_APPS": str(root),
        "SKELETON_BUNDLED_APPS": "",
        "PYTHONPATH": str(ROOT),
    }
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, env=environment, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr[-3000:]
    assert result.stdout.split()[-3:] == ["True", "False", "ledger"]


def test_test_modules_of_a_checkout_map_to_the_managed_name(tmp_path):
    from skeleton.conf.dev_apps import managed_module_name, registered_checkouts

    root = checkout(tmp_path)
    dev_app_configs([root])
    assert registered_checkouts()["acme_devapp"] == root.resolve()
    assert (
        managed_module_name(root / "tests" / "test_service_api.py") == "applications.acme_devapp.tests.test_service_api"
    )
    assert managed_module_name(root / "tests" / "__init__.py") == "applications.acme_devapp.tests"
    (root / "loose").mkdir()
    assert managed_module_name(root / "loose" / "test_x.py") is None  # not a package
    assert managed_module_name(tmp_path / "elsewhere.py") is None
    assert managed_module_name(root / "README.md") is None
