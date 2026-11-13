"""Instance configuration in its own SQLite with encrypted secrets (#143)."""

import json
import os
import sqlite3
import stat
import subprocess
import sys
from io import StringIO
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured
from django.core.management import CommandError, call_command

from skeleton.instance import crypto
from skeleton.instance.resolve import instance_env, instance_paths, missing_for_setup, resolve_instance_settings
from skeleton.instance.schema import BOOTSTRAP_ENV, BY_ENV, CATALOG, spec_for
from skeleton.instance.store import InstanceStore

ROOT = Path(__file__).resolve().parents[2]
SECRET = "Only-in-the-store-Pass-2026-" + "abcdefghijklmnopqrstuvwxyz"
DATABASE_PASSWORD = "db-password-never-plain"
MFA_KEYS = (Fernet.generate_key().decode(), Fernet.generate_key().decode())


@pytest.fixture
def instance(tmp_path, monkeypatch):
    database, key = tmp_path / "instance" / "instance.sqlite3", tmp_path / "keys" / "master.key"
    monkeypatch.setenv("SKELETON_INSTANCE_DB", str(database))
    monkeypatch.setenv("SKELETON_INSTANCE_KEY_FILE", str(key))
    for name in BY_ENV:
        monkeypatch.delenv(name, raising=False)
    return database, key


def run(*args, stdin=None, monkeypatch=None):
    output = StringIO()
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", StringIO(stdin))
    call_command("instance", *args, stdout=output)
    return output.getvalue()


def test_catalog_is_closed_and_validates_types():
    assert spec_for("DJANGO_SECRET_KEY") is CATALOG["secret_key"]
    with pytest.raises(KeyError):
        spec_for("SKELETON_UNKNOWN")
    assert not set(BOOTSTRAP_ENV) & set(BY_ENV), "Bootstrap variables never live in the store."
    assert "MIGRATION_DATABASE_URL" not in BY_ENV, "The owner credential is never stored."
    assert CATALOG["trust_proxy"].clean("TRUE") == "true"
    assert CATALOG["json_max_bytes"].clean(" 10 ") == "10"
    assert CATALOG["allowed_hosts"].clean("a.example, b.example") == "a.example,b.example"
    for key, value in (
        ("trust_proxy", "maybe"),
        ("json_max_bytes", "0"),
        ("allowed_hosts", "a,,b"),
        ("database_url", "not a url"),
        ("files_backend", "ftp"),
        ("secret_key", "line\nbreak"),
    ):
        with pytest.raises(ValueError):
            CATALOG[key].clean(value)
    assert {spec.key for spec in CATALOG.values() if spec.secret} >= {
        "secret_key",
        "database_url",
        "email_url",
        "cache_url",
        "mfa_encryption_keys",
        "identity_provider_encryption_keys",
        "integration_encryption_keys",
    }


def test_set_encrypts_secrets_and_show_masks_them(instance, monkeypatch):
    database, key = instance
    run("set", "secret_key", "--stdin", stdin=SECRET + "\n", monkeypatch=monkeypatch)
    run("set", "DATABASE_URL", f"postgres://runtime:{DATABASE_PASSWORD}@db.example:5432/app")
    run("set", "allowed_hosts", "app.example")
    raw = database.read_bytes()
    assert SECRET.encode() not in raw and DATABASE_PASSWORD.encode() not in raw
    assert b"app.example" in raw  # Non-secret values stay readable for support.
    shown = run("show")
    assert SECRET not in shown and DATABASE_PASSWORD not in shown
    assert "secret_key [DJANGO_SECRET_KEY] = ****** (definido)  <instance>" in shown
    assert "allowed_hosts [DJANGO_ALLOWED_HOSTS] = app.example  <instance>" in shown
    assert "health_host [DJANGO_HEALTH_HOST] = (padrão)  <default>" in shown
    with sqlite3.connect(database) as connection:
        log = connection.execute("SELECT key, action, source FROM change_log ORDER BY id").fetchall()
    assert log == [
        ("secret_key", "create", "cli:set"),
        ("database_url", "create", "cli:set"),
        ("allowed_hosts", "create", "cli:set"),
    ]
    assert SECRET.encode() not in key.read_bytes()


def test_set_refuses_unknown_keys_and_invalid_values(instance):
    database, _ = instance
    with pytest.raises(CommandError, match="Chave desconhecida"):
        run("set", "SKELETON_UNKNOWN", "x")
    with pytest.raises(CommandError, match="true ou false"):
        run("set", "trust_proxy", "maybe")
    with pytest.raises(CommandError):
        run("set", "MIGRATION_DATABASE_URL", "postgres://owner:x@db/app")
    with pytest.raises(CommandError):
        run("set", "secret_key")  # Neither a value nor --stdin.
    assert InstanceStore(database).settings() == {}


def test_environment_wins_over_the_store_and_the_origin_is_visible(instance, monkeypatch):
    run("set", "allowed_hosts", "store.example")
    run("set", "secret_key", SECRET)
    env = instance_env(ROOT)
    assert env.list("DJANGO_ALLOWED_HOSTS") == ["store.example"]
    assert env("DJANGO_SECRET_KEY") == SECRET
    assert env("DJANGO_HEALTH_HOST", default="fallback") == "fallback"
    monkeypatch.setenv("DJANGO_ALLOWED_HOSTS", "env.example")
    env = instance_env(ROOT)
    assert env.list("DJANGO_ALLOWED_HOSTS") == ["env.example"]
    origins = {item.key: item.origin for item in resolve_instance_settings(ROOT)}
    assert origins["allowed_hosts"] == "env" and origins["secret_key"] == "instance"
    assert origins["health_host"] == "default"
    assert "allowed_hosts [DJANGO_ALLOWED_HOSTS] = env.example  <env>" in run("show")
    # Names outside the catalog keep reading the environment only.
    monkeypatch.setenv("SKELETON_SOMETHING_ELSE", "plain")
    assert instance_env(ROOT)("SKELETON_SOMETHING_ELSE") == "plain"


def test_missing_master_key_fails_clearly_without_falling_back(instance, monkeypatch):
    _, key = instance
    run("set", "secret_key", SECRET)
    key.unlink()
    env = instance_env(ROOT)
    with pytest.raises(ImproperlyConfigured, match="Chave mestra da instância ausente"):
        env("DJANGO_SECRET_KEY", default="development-only-not-for-deployment")
    # An environment override does not need the key, and non-secret keys still read.
    monkeypatch.setenv("DJANGO_SECRET_KEY", "from-environment")
    assert instance_env(ROOT)("DJANGO_SECRET_KEY") == "from-environment"
    # A different key does not decrypt either: no silent default.
    monkeypatch.delenv("DJANGO_SECRET_KEY")
    crypto.load_key(key, create=True)
    with pytest.raises(ImproperlyConfigured, match="não decifra"):
        instance_env(ROOT)("DJANGO_SECRET_KEY")


def test_import_env_unset_and_template(instance, monkeypatch):
    database, _ = instance
    monkeypatch.setenv("DJANGO_SECRET_KEY", SECRET)
    monkeypatch.setenv("DJANGO_ALLOWED_HOSTS", "imported.example")
    monkeypatch.setenv("SKELETON_INSTANCE_UNRELATED", "ignored")
    output = run("import-env")
    assert "allowed_hosts" in output and "secret_key" in output and SECRET not in output
    stored = InstanceStore(database).settings()
    assert set(stored) == {"secret_key", "allowed_hosts"}
    assert stored["secret_key"][1] is True and SECRET not in stored["secret_key"][0]
    monkeypatch.setenv("DJANGO_TRUST_PROXY", "maybe")
    with pytest.raises(CommandError, match="DJANGO_TRUST_PROXY"):
        run("import-env")
    assert "removida" in run("unset", "allowed_hosts")
    assert "allowed_hosts" not in InstanceStore(database).settings()
    template = run("export-template")
    assert "# DJANGO_SECRET_KEY=" in template and "# SKELETON_INSTANCE_DB=" in template
    assert SECRET not in template and "imported.example" not in template


def test_required_key_is_not_unset_without_force(instance):
    """Without the environment, removing a required key would leave the instance unable to start (#267)."""
    run("set", "allowed_hosts", "kept.example")
    with pytest.raises(CommandError, match="--force"):
        run("unset", "allowed_hosts")
    assert "allowed_hosts" in InstanceStore(instance[0]).settings()
    assert "removida" in run("unset", "allowed_hosts", "--force")
    assert "allowed_hosts" not in InstanceStore(instance[0]).settings()


def test_can_clear_follows_environment(instance):
    from skeleton.instance.writer import can_clear, clear_setting, write_setting

    assert not can_clear(spec_for("secret_key"), {})
    assert can_clear(spec_for("secret_key"), {"DJANGO_SECRET_KEY": "x"})
    assert can_clear(spec_for("default_from_email"), {})
    write_setting(ROOT, "allowed_hosts", "kept.example", source="test")
    with pytest.raises(ValueError, match="obrigatória"):
        clear_setting(ROOT, "allowed_hosts", source="test")
    assert clear_setting(ROOT, "allowed_hosts", source="test", allow_required=True)


def test_status_lists_what_the_setup_still_needs(instance):
    assert missing_for_setup(ROOT) == ["secret_key", "allowed_hosts", "database_url"]
    run("set", "secret_key", SECRET)
    status = run("status")
    assert "Faltam para o setup: allowed_hosts, database_url" in status
    assert "Versão do schema: 1" in status


def test_store_and_key_must_be_separate_files(tmp_path):
    same = tmp_path / "one.file"
    with pytest.raises(crypto.InstanceKeyError):
        instance_paths(ROOT, {"SKELETON_INSTANCE_DB": str(same), "SKELETON_INSTANCE_KEY_FILE": str(same)})


@pytest.mark.posix
def test_store_and_master_key_are_created_private(instance):
    database, key = instance
    run("set", "secret_key", SECRET)
    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    assert stat.S_IMODE(key.stat().st_mode) == 0o600


def test_settings_start_from_the_store_without_env_file(instance):
    """A production settings load in a fresh process, with every catalog value from the store."""
    database, key = instance
    values = {
        "secret_key": SECRET,
        "allowed_hosts": "app.example",
        "database_url": f"postgres://runtime:{DATABASE_PASSWORD}@db.example:5432/app",
        "cache_url": "rediscache://cache.example:6379/1",
        "email_url": "smtp+tls://mailer:mail-pass@mail.example:587",
        "default_from_email": "app@example.org",
        "mfa_encryption_keys": ",".join(MFA_KEYS),
    }
    for name, value in values.items():
        run("set", name, value)
    environment = {
        name: value
        for name, value in os.environ.items()
        if name not in BY_ENV and not name.startswith(("TEST_", "DJANGO_", "SKELETON_"))
    }
    environment.update(
        {
            "DJANGO_SETTINGS_MODULE": "config.settings.production",
            "SKELETON_INSTANCE_DB": str(database),
            "SKELETON_INSTANCE_KEY_FILE": str(key),
            "SKELETON_PROJECT_ROOT": str(ROOT),
        }
    )
    script = (
        "import json, django; django.setup(); from django.conf import settings as s; "
        f"print(json.dumps([s.SECRET_KEY == {SECRET!r}, s.ALLOWED_HOSTS, s.DATABASES['default']['HOST'], "
        f"s.DATABASES['default']['PASSWORD'] == {DATABASE_PASSWORD!r}, s.CACHES['default']['BACKEND'], "
        "s.EMAIL_HOST, s.MFA_ENCRYPTION_KEYS, s.DEFAULT_FROM_EMAIL]))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, env=environment, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert json.loads(result.stdout.strip().splitlines()[-1]) == [
        True,
        ["app.example"],
        "db.example",
        True,
        "django.core.cache.backends.redis.RedisCache",
        "mail.example",
        list(MFA_KEYS),
        "app@example.org",
    ]
    assert SECRET not in result.stdout + result.stderr


KEYRINGS = ("mfa_encryption_keys", "identity_provider_encryption_keys", "integration_encryption_keys")


@pytest.mark.parametrize("name", KEYRINGS)
def test_keyrings_must_be_fernet_keys(name):
    """A keyring item that is not a Fernet key is refused on write, not on first use (#323)."""
    with pytest.raises(ValueError, match="chave Fernet"):
        CATALOG[name].clean("abc")
    first, second = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    assert CATALOG[name].clean(f" {first} , {second} ") == f"{first},{second}"


def test_secret_key_has_a_minimum_strength():
    for weak in ("x" * 49, "x" * 60, "abcd" * 20):
        with pytest.raises(ValueError, match="50 caracteres"):
            CATALOG["secret_key"].clean(weak)
    assert CATALOG["secret_key"].clean(SECRET) == SECRET


def test_invalid_keyring_message_does_not_echo_the_value():
    refused = "not-a-key-but-maybe-a-secret-2026"
    with pytest.raises(ValueError) as error:
        CATALOG["integration_encryption_keys"].clean(f"{Fernet.generate_key().decode()},{refused}")
    assert refused not in str(error.value)


def test_concurrent_first_use_reads_a_complete_key(tmp_path):
    """Racing first uses all get the same, complete key; no temporary file is left (#323)."""
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "keys" / "master.key"
    with ThreadPoolExecutor(max_workers=8) as pool:
        keys = list(pool.map(lambda _: crypto.load_key(path, create=True), range(16)))
    token = crypto.encrypt(keys[0], "secret")
    assert all(crypto.decrypt(key, token) == "secret" for key in keys)
    assert list(path.parent.glob("*.tmp")) == []


def test_missing_key_with_stored_secrets_refuses_to_create_another(instance):
    """A new key would strand the secrets already stored: restore the backup instead (#323)."""
    from skeleton.instance.writer import write_setting

    database, key = instance
    write_setting(ROOT, "secret_key", SECRET, source="test")
    key.unlink()
    with pytest.raises(crypto.MasterKeyMissing, match="restaure"):
        write_setting(ROOT, "email_url", "smtp+tls://user:pass@mail.example:587", source="test")
    assert not key.exists()
    assert "email_url" not in InstanceStore(database).settings()
    with pytest.raises(CommandError, match="restaure"):
        run("set", "email_url", "smtp+tls://user:pass@mail.example:587")
    assert not key.exists()
    # A non-secret key does not need the master key.
    write_setting(ROOT, "allowed_hosts", "app.example", source="test")
    assert InstanceStore(database).settings()["allowed_hosts"] == ("app.example", False)


def test_missing_key_without_stored_secrets_is_created(instance):
    from skeleton.instance.writer import write_setting

    database, key = instance
    write_setting(ROOT, "allowed_hosts", "app.example", source="test")
    assert not key.exists()
    write_setting(ROOT, "secret_key", SECRET, source="test")
    assert key.is_file()
    stored = InstanceStore(database).settings()["secret_key"][0]
    assert crypto.decrypt(crypto.load_key(key), stored) == SECRET
