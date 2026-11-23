"""Cluster mode of the instance (#673): configuration from the environment only, no writes, export-env."""

import base64
import json
import os
import stat
import subprocess
import sys
from io import StringIO
from pathlib import Path

import environ
import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import CommandError, call_command

from skeleton.instance.resolve import instance_env, instance_mode, resolve_instance_settings
from skeleton.instance.schema import BY_ENV
from skeleton.instance.store import InstanceStore
from skeleton.instance.writer import InstanceClusterMode, export_environment, write_setting

ROOT = Path(__file__).resolve().parents[2]
SECRET = "Only-in-the-store-Pass-2026-" + "abcdefghijklmnopqrstuvwxyz"


@pytest.fixture
def instance(tmp_path, monkeypatch):
    database, key = tmp_path / "instance" / "instance.sqlite3", tmp_path / "keys" / "master.key"
    monkeypatch.setenv("SKELETON_INSTANCE_DB", str(database))
    monkeypatch.setenv("SKELETON_INSTANCE_KEY_FILE", str(key))
    monkeypatch.delenv("SKELETON_INSTANCE_MODE", raising=False)
    for name in BY_ENV:
        monkeypatch.delenv(name, raising=False)
    return database, key


def test_the_mode_is_standalone_by_default_and_refuses_unknown_values():
    assert instance_mode({}) == "standalone"
    assert instance_mode({"SKELETON_INSTANCE_MODE": " Cluster "}) == "cluster"
    with pytest.raises(ImproperlyConfigured):
        instance_mode({"SKELETON_INSTANCE_MODE": "replica"})


def test_cluster_mode_ignores_the_stored_settings(instance, monkeypatch, settings):
    write_setting(settings.BASE_DIR, "default_from_email", "stored@example.test", source="test")
    assert instance_env(settings.BASE_DIR)("DEFAULT_FROM_EMAIL") == "stored@example.test"
    monkeypatch.setenv("SKELETON_INSTANCE_MODE", "cluster")
    assert instance_env(settings.BASE_DIR)("DEFAULT_FROM_EMAIL", default="none") == "none"
    origins = {item.key: item.origin for item in resolve_instance_settings(settings.BASE_DIR)}
    assert origins["default_from_email"] == "default"
    monkeypatch.setenv("DEFAULT_FROM_EMAIL", "env@example.test")
    assert instance_env(settings.BASE_DIR)("DEFAULT_FROM_EMAIL") == "env@example.test"


def test_cluster_mode_never_enters_the_setup(instance, monkeypatch, settings):
    from skeleton.setup.mode import setup_required

    assert setup_required(settings.BASE_DIR)  # Nothing configured: standalone falls into the setup.
    monkeypatch.setenv("SKELETON_INSTANCE_MODE", "cluster")
    assert not setup_required(settings.BASE_DIR)


def test_every_write_is_refused_in_cluster_mode(instance, monkeypatch, settings):
    database, _ = instance
    monkeypatch.setenv("SKELETON_INSTANCE_MODE", "cluster")
    with pytest.raises(InstanceClusterMode):
        write_setting(settings.BASE_DIR, "default_from_email", "x@example.test", source="test")
    for args in (
        ("set", "default_from_email", "x@example.test"),
        ("unset", "default_from_email"),
        ("import-env",),
        ("rotate-key",),
        ("setup-token",),
    ):
        with pytest.raises(CommandError, match="instance_cluster_mode"):
            call_command("instance", *args, stdout=StringIO())
    assert not database.exists()
    output = StringIO()
    call_command("instance", "status", stdout=output)
    assert "Modo: cluster" in output.getvalue()


def test_export_env_writes_a_private_file_that_reproduces_the_configuration(instance, tmp_path, settings):
    write_setting(settings.BASE_DIR, "secret_key", SECRET, source="test")
    write_setting(settings.BASE_DIR, "default_from_email", 'ops "quoted" $HOME \\ <x@example.test>', source="test")
    os.environ["DJANGO_ALLOWED_HOSTS"] = "app.example.test"
    try:
        target = tmp_path / "cluster.env"
        output = StringIO()
        call_command("instance", "export-env", "--output", str(target), stdout=output)
    finally:
        del os.environ["DJANGO_ALLOWED_HOSTS"]
    text = target.read_text(encoding="utf-8")
    assert "DJANGO_ALLOWED_HOSTS=" in text and "DJANGO_SECRET_KEY=" in text
    assert SECRET in text and SECRET not in output.getvalue()
    if os.name == "posix":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    parsed: dict[str, str] = {}
    environ.Env.read_env(str(target), overwrite=True, **{})  # Fills os.environ: read back into a copy.
    for name in ("DJANGO_SECRET_KEY", "DEFAULT_FROM_EMAIL", "DJANGO_ALLOWED_HOSTS"):
        parsed[name] = os.environ.pop(name)
    assert parsed["DJANGO_SECRET_KEY"] == SECRET
    assert parsed["DJANGO_ALLOWED_HOSTS"] == "app.example.test"
    # The change log records the export, never a value.
    changes = InstanceStore(instance[0]).changes()
    assert ("*", "export-env", "cli:export-env") in [entry[:3] for entry in changes]
    assert SECRET not in repr(changes)
    with pytest.raises(CommandError, match="export_target_exists"):
        call_command("instance", "export-env", "--output", str(target), stdout=StringIO())
    with pytest.raises(CommandError, match="export_requires_file"):
        call_command("instance", "export-env", "--output", "-", stdout=StringIO())


def test_export_env_quotes_for_compose_and_systemd(instance, tmp_path, settings):
    write_setting(settings.BASE_DIR, "default_from_email", 'a"b$c\\d', source="test")
    target = tmp_path / "quoted.env"
    export_environment(settings.BASE_DIR, target, source="test")
    assert 'DEFAULT_FROM_EMAIL="a\\"b\\$c\\\\d"' in target.read_text(encoding="utf-8")


def test_export_env_is_refused_in_cluster_mode(instance, tmp_path, monkeypatch, settings):
    monkeypatch.setenv("SKELETON_INSTANCE_MODE", "cluster")
    with pytest.raises(InstanceClusterMode):
        export_environment(settings.BASE_DIR, tmp_path / "x.env", source="test")
    assert not (tmp_path / "x.env").exists()


def test_cluster_checks_require_shared_files_and_warn_about_stored_settings(instance, settings):
    from skeleton.instance.checks import cluster_problems

    assert cluster_problems() == []  # Standalone: nothing to say.
    write_setting(settings.BASE_DIR, "default_from_email", "stored@example.test", source="test")
    settings.SKELETON_INSTANCE_MODE = "cluster"
    settings.SKELETON_FILES_SHARED = False
    local = {"BACKEND": "django.core.files.storage.FileSystemStorage", "OPTIONS": {"location": "/files"}}
    settings.STORAGES = {**settings.STORAGES, "skeleton_files": local}
    assert {problem.id for problem in cluster_problems()} == {"skeleton.E048", "skeleton.W048"}
    settings.SKELETON_FILES_SHARED = True
    assert {problem.id for problem in cluster_problems()} == {"skeleton.W048"}
    settings.SKELETON_FILES_SHARED = False
    settings.STORAGES = {**settings.STORAGES, "skeleton_files": {"BACKEND": "storages.backends.s3.S3Storage"}}
    assert {problem.id for problem in cluster_problems()} == {"skeleton.W048"}


def test_production_in_cluster_mode_names_the_missing_keys(tmp_path):
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in BY_ENV and not key.startswith(("TEST_", "DJANGO_SETTINGS"))
    }
    environment.update(
        DJANGO_SETTINGS_MODULE="config.settings.production",
        SKELETON_INSTANCE_MODE="cluster",
        SKELETON_INSTANCE_DB=str(tmp_path / "instance.sqlite3"),
        SKELETON_INSTANCE_KEY_FILE=str(tmp_path / "master.key"),
        PYTHONIOENCODING="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-c", "import django; django.setup()"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode != 0
    assert "Modo cluster: faltam no ambiente as chaves" in result.stderr and "database_url" in result.stderr
    assert not (tmp_path / "instance.sqlite3").exists()


def test_the_exported_file_starts_a_cluster_node_with_the_same_configuration(instance, tmp_path, settings, monkeypatch):
    """#673: the file of export-env, as the env_file of a node, gives the same keys: sessions stay valid."""
    from django.utils.crypto import salted_hmac

    database, key = instance
    database_password = "db-password-never-plain"
    values = {
        "secret_key": SECRET,
        "allowed_hosts": "app.example",
        "database_url": f"postgres://runtime:{database_password}@db.example:5432/app",
        "cache_url": "rediscache://cache.example:6379/1",
        "cache_key_prefix": "inst-a",
        "email_url": "smtp+tls://mailer:mail-pass@mail.example:587",
        "default_from_email": "app@example.org",
        "mfa_encryption_keys": base64.urlsafe_b64encode(bytes(range(32))).decode(),
    }
    for name, value in values.items():
        write_setting(settings.BASE_DIR, name, value, source="test")
    exported = tmp_path / "cluster.env"
    export_environment(settings.BASE_DIR, exported, source="test")
    # The SQLite changes afterwards: a cluster node never reads it.
    write_setting(settings.BASE_DIR, "default_from_email", "changed@example.org", source="test")
    parsed: dict[str, str] = {}
    monkeypatch.setattr(environ.Env, "ENVIRON", parsed)
    environ.Env.read_env(str(exported), overwrite=True)
    environment = {
        name: value
        for name, value in os.environ.items()
        if name not in BY_ENV and not name.startswith(("TEST_", "DJANGO_", "SKELETON_"))
    }
    environment.update(
        parsed,
        DJANGO_SETTINGS_MODULE="config.settings.production",
        SKELETON_INSTANCE_MODE="cluster",
        SKELETON_INSTANCE_DB=str(database),
        SKELETON_INSTANCE_KEY_FILE=str(key),
        SKELETON_PROJECT_ROOT=str(ROOT),
        PYTHONIOENCODING="utf-8",
    )
    session_salt = "django.contrib.auth.models.AbstractBaseUser.get_session_auth_hash"
    script = (
        "import json, django; django.setup(); from django.conf import settings as s; "
        "from django.utils.crypto import salted_hmac; "
        f"print(json.dumps([salted_hmac({session_salt!r}, 'password-hash', algorithm='sha256').hexdigest(), "
        "s.ALLOWED_HOSTS, s.DATABASES['default']['HOST'], s.CACHES['default']['KEY_PREFIX'], s.DEFAULT_FROM_EMAIL]))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, env=environment, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr[-2000:]
    expected_hash = salted_hmac(session_salt, "password-hash", secret=SECRET, algorithm="sha256").hexdigest()
    assert json.loads(result.stdout.strip().splitlines()[-1]) == [
        expected_hash,  # The session auth hash of a user logged in before: the session stays valid.
        ["app.example"],
        "db.example",
        "inst-a",
        "app@example.org",
    ]
    assert SECRET not in result.stdout + result.stderr
