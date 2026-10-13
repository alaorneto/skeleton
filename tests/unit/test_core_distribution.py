"""Build/install a real core-only wheel and exercise signed-base source adoption."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from skeleton.distribution import adopt_source_core, build_core_release, source_adoption_plan, wheel_inventory
from skeleton.system.packages import PackageError, validate_package

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def core_wheel(tmp_path_factory):
    output = tmp_path_factory.mktemp("core-wheel")
    subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "--wheel-dir", str(output), str(ROOT)],
                   cwd=output, check=True, capture_output=True, text=True)
    wheels = list(output.glob("*.whl"))
    assert len(wheels) == 1
    return wheels[0]


@pytest.fixture
def signed_core(core_wheel, tmp_path):
    base = wheel_inventory(core_wheel)
    key = Ed25519PrivateKey.generate()
    private = tmp_path / "publisher.pem"
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    manifest = {"manifest_version": 1, "kind": "core", "component_id": "django-skeleton", "name": "Skeleton",
                "description": "Test core release", "version": base["version"], "publisher": "fixture-publisher",
                "compatibility": {"skeleton": ">=0.1.0,<1.0.0", "python": ">=3.12,<3.15", "django": ">=5.2,<5.3"},
                "dependencies": {}, "migrations": {"leaves": [], "from_versions": ">=0.1.0"}, "files": [],
                "python_dependencies": [{"name": "django-skeleton", "version": base["version"],
                                         "wheel": "wheels/" + core_wheel.name, "sha256": "0" * 64}],
                "lifecycle": {"data_retention": "retain", "maintenance": True, "backup": True}}
    source = tmp_path / "manifest.json"
    source.write_text(json.dumps(manifest))
    release = build_core_release(core_wheel, source, tmp_path / "core.skapp", private)
    return validate_package(release, {"fixture-publisher": key.public_key()})


def unpack_core(wheel, destination):
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if name.startswith("skeleton/") and not name.endswith("/"):
                path = destination / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(archive.read(name))


def test_real_wheel_contains_only_core_and_preserves_historical_resources(core_wheel):
    base = wheel_inventory(core_wheel)
    assert "skeleton/accounts/migrations/0001_initial.py" in base["files"]
    assert "skeleton/system/migrations/0002_technical_privileges.py" in base["files"]
    assert any("/templates/" in name for name in base["files"])
    assert any("/static/" in name for name in base["files"])
    with zipfile.ZipFile(core_wheel) as archive:
        assert all(name.startswith("skeleton/") or ".dist-info/" in name for name in archive.namelist())


def test_wheel_installs_outside_checkout_and_all_project_profiles_start(core_wheel, tmp_path):
    target = tmp_path / "installed"
    subprocess.run([sys.executable, "-m", "pip", "install", "--no-deps", "--target", str(target), str(core_wheel)],
                   cwd=tmp_path, check=True, capture_output=True, text=True)
    project = tmp_path / "project"
    project.mkdir()
    shutil.copytree(ROOT / "config", project / "config", ignore=shutil.ignore_patterns("__pycache__"))
    (project / "applications").mkdir()
    (project / "applications/__init__.py").write_text("")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(project), str(target)]), "SKELETON_PROJECT_ROOT": str(project),
           "SKELETON_ENABLE_EXAMPLE_APP": "false", "DJANGO_SECRET_KEY": "fixture-only", "DJANGO_ALLOWED_HOSTS": "testserver",
           "DATABASE_URL": "postgres://skeleton_runtime:fixture@127.0.0.1:5433/skeleton_test", "CACHE_URL": "rediscache://127.0.0.1:6380/0",
           "EMAIL_URL": "smtp://127.0.0.1:2525", "DEFAULT_FROM_EMAIL": "fixture@example.test",
           "MFA_ENCRYPTION_KEYS": "dV6nwLEp9c3A8-yiLskMRFbJaUMoXXJ-_SNkrulBj8I="}
    for key in ("SSO_PROVIDER_CONFIG_PATH", "SKELETON_DEPLOYMENT_INVENTORY", "SKELETON_MAINTENANCE_MARKER"):
        env.pop(key, None)
    code = """
import django
from pathlib import Path
django.setup()
from django.apps import apps
from django.conf import settings
from skeleton.versioning import core_version
import skeleton
assert Path(skeleton.__file__).resolve().is_relative_to(Path(TARGET))
assert settings.BASE_DIR == Path(PROJECT)
assert settings.AUTH_USER_MODEL == 'sk_accounts.User'
assert apps.get_app_config('sk_accounts').name == 'skeleton.accounts'
assert core_version()['origin'] == 'installed'
from django.template.loader import get_template
get_template('account/login.html')
""".replace("TARGET", repr(str(target))).replace("PROJECT", repr(str(project)))
    for profile in ("development", "test", "build", "migrate", "production"):
        env["DJANGO_SETTINGS_MODULE"] = "config.settings." + profile
        result = subprocess.run([sys.executable, "-c", code], cwd=project, env=env, capture_output=True, text=True)
        assert result.returncode == 0, profile + ": " + result.stderr


def test_signed_base_detects_local_modification_without_overwrite(core_wheel, signed_core, tmp_path):
    project = tmp_path / "project"
    unpack_core(core_wheel, project)
    assert source_adoption_plan(project, signed_core)["classification"] == "equivalent"
    original = project / "skeleton/accounts/migrations/0001_initial.py"
    original.write_text(original.read_text() + "\n# local fork\n")
    plan = source_adoption_plan(project, signed_core)
    assert plan["classification"] == "fork"
    assert "skeleton/accounts/migrations/0001_initial.py" in plan["changed"]
    with pytest.raises(PackageError, match="Locally modified"):
        adopt_source_core(project, signed_core, tmp_path / "backup")
    assert original.exists()


def test_adoption_removes_only_equivalent_source_and_retains_migrations(core_wheel, signed_core, tmp_path, monkeypatch):
    project, external = tmp_path / "project", tmp_path / "site-packages"
    unpack_core(core_wheel, project)
    unpack_core(core_wheel, external)
    preserved = {"applications/business.py": b"business", "config/settings/project.py": b"project overlay", "manage.py": b"entrypoint"}
    for name, content in preserved.items():
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    class Installed:
        version = wheel_inventory(core_wheel)["version"]
        def locate_file(self, path):
            return external / path
    monkeypatch.setattr("skeleton.distribution.importlib.metadata.distribution", lambda name: Installed())
    result = adopt_source_core(project, signed_core, tmp_path / "backups")
    assert not (project / "skeleton").exists()
    assert (Path(result["backup"]) / "accounts/migrations/0001_initial.py").is_file()
    assert all((project / name).read_bytes() == content for name, content in preserved.items())


def test_worker_confirmation_checks_actual_loaded_core_bytes(monkeypatch):
    from skeleton.deployment import DeploymentInventory, confirm_core
    import skeleton.deployment
    source = Path(skeleton.deployment.__file__)
    payload = source.read_bytes()
    files = {"skeleton/deployment.py": {"sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}}
    state = DeploymentInventory(5, {"version": "0.1.0", "digest": "a" * 64, "files": files}, (), (), ())
    monkeypatch.setattr("skeleton.versioning.core_version", lambda: {"version": "0.1.0", "origin": "installed"})
    assert confirm_core(state)[1] is True
    files["skeleton/deployment.py"]["sha256"] = "0" * 64
    assert confirm_core(state)[1] is False
    files["skeleton/deployment.py"]["sha256"] = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr("skeleton.versioning.core_version", lambda: {"version": "0.1.0", "origin": "source"})
    assert confirm_core(state)[1] is False
