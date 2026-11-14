"""Setup mode choice, single-use token and DSN parsing, without a database (#146)."""

import io
import os
import signal
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from skeleton.instance.schema import BY_ENV
from skeleton.instance.store import InstanceStore
from skeleton.setup import database, mode, tokens

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def store(tmp_path):
    return InstanceStore(tmp_path / "instance" / "instance.sqlite3")


def test_token_is_issued_once_printed_and_written_0600(store):
    stream = io.StringIO()
    token = tokens.issue_token(store, stream=stream)
    assert token and token in stream.getvalue()
    path = tokens.token_file(store)
    assert path.read_text().strip() == token
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
    assert tokens.issue_token(store, stream=io.StringIO()) is None  # An open token exists.
    assert token not in store.path.read_bytes().decode("latin-1")  # Only the digest is stored.


def test_wrong_reused_and_expired_tokens_are_refused(store):
    now = datetime(2026, 11, 10, tzinfo=UTC)
    token = tokens.issue_token(store, now=now, stream=io.StringIO())
    assert not tokens.consume_token(store, "x" * 32, now=now)
    assert not tokens.consume_token(store, "short", now=now)
    assert tokens.consume_token(store, token, now=now)
    assert not tokens.consume_token(store, token, now=now)  # Single use.
    assert not tokens.token_file(store).exists()
    later = now + timedelta(hours=1)
    fresh = tokens.issue_token(store, now=later, stream=io.StringIO())
    assert fresh and not tokens.consume_token(store, fresh, now=later + tokens.TOKEN_LIFETIME + timedelta(seconds=1))


def test_setup_mode_follows_the_required_keys(tmp_path, monkeypatch):
    base = {"SKELETON_INSTANCE_DB": str(tmp_path / "i.sqlite3"), "SKELETON_INSTANCE_KEY_FILE": str(tmp_path / "k")}
    assert mode.setup_required(tmp_path, base)
    configured = {
        **base,
        "DJANGO_SECRET_KEY": "x" * 50,
        "DJANGO_ALLOWED_HOSTS": "app.example.test",
        "DATABASE_URL": "postgres://u:p@db/x",
    }
    assert not mode.setup_required(tmp_path, configured)
    assert not mode.setup_required(tmp_path, {**base, "SKELETON_SETUP": "off"})
    monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)
    for name, value in base.items():
        monkeypatch.setenv(name, value)
    # SKELETON_SETUP_TARGET too: choosing the setup records it, and monkeypatch removes it afterwards.
    for name in ("DJANGO_SECRET_KEY", "DJANGO_ALLOWED_HOSTS", "DATABASE_URL", "SKELETON_SETUP", mode.SETUP_TARGET_ENV):
        monkeypatch.delenv(name, raising=False)
    assert mode.select_settings_module("config.settings.production", base_dir=tmp_path) == mode.SETUP_SETTINGS
    assert os.environ[mode.SETUP_TARGET_ENV] == "config.settings.production"
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "config.settings.test")
    assert mode.select_settings_module("config.settings.production", base_dir=tmp_path) == "config.settings.test"


def test_the_wsgi_entrypoint_issues_and_prints_the_token_in_a_fresh_process(tmp_path):
    """Gunicorn imports ``config.wsgi`` before the apps exist; issuing the token must not translate (#455)."""
    environment = {
        name: value
        for name, value in os.environ.items()
        if name not in BY_ENV and not name.startswith(("DJANGO_", "SKELETON_", "TEST_"))
    }
    database = tmp_path / "instance" / "instance.sqlite3"
    environment.update(
        {"SKELETON_INSTANCE_DB": str(database), "SKELETON_INSTANCE_KEY_FILE": str(tmp_path / "keys" / "master.key")}
    )

    def start():
        return subprocess.run(
            [sys.executable, "-c", "import config.wsgi"],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
        )

    first = start()
    assert first.returncode == 0, first.stderr
    token = tokens.token_file(InstanceStore(database)).read_text().strip()
    assert f"Token de primeiro acesso (uso único, vale 24 h): {token}" in first.stderr
    second = start()  # The token is still open: no other token, nothing printed.
    assert second.returncode == 0, second.stderr
    assert "Token de primeiro acesso" not in second.stderr


def test_a_restart_in_the_middle_of_the_setup_stays_in_setup_mode(tmp_path):
    """The first steps store every required key; only the completion leaves setup mode (#453)."""
    database = tmp_path / "instance" / "i.sqlite3"
    base = {"SKELETON_INSTANCE_DB": str(database), "SKELETON_INSTANCE_KEY_FILE": str(tmp_path / "k")}
    configured = {
        **base,
        "DJANGO_SECRET_KEY": "x" * 50,
        "DJANGO_ALLOWED_HOSTS": "app.example.test",
        "DATABASE_URL": "postgres://u:p@db/x",
    }
    assert not mode.setup_required(tmp_path, configured)
    assert not database.exists()  # Reading the state never creates the instance store.
    store = InstanceStore(database)
    tokens.issue_token(store, stream=io.StringIO())
    # A token alone (an instance configured by variables or the CLI after it started empty) is not a setup.
    assert not mode.setup_required(tmp_path, configured)
    store.put_setup_state("step:instance", "done")
    store.put_setup_state("step:database", "done")
    assert mode.setup_required(tmp_path, configured)
    assert not mode.setup_required(tmp_path, {**configured, "SKELETON_SETUP": "off"})
    store.put_setup_state("status", mode.COMPLETED)
    assert not mode.setup_required(tmp_path, configured)


@pytest.mark.parametrize(
    "dsn", ["sqlite:///db.sqlite3", "mysql://u:p@h/db", "postgres://host-only", "postgres:///nodb", 42]
)
def test_only_complete_postgresql_urls_are_accepted(dsn):
    with pytest.raises(database.DatabaseCheckError):
        database.parse(dsn)


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        ({"SKELETON_SETUP_RESTART": "exit"}, "exit"),
        pytest.param(
            {"SERVER_SOFTWARE": "gunicorn/23.0.0"},
            "gunicorn",
            marks=pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="SIGHUP is POSIX only"),
        ),
        ({"RUN_MAIN": "true"}, "autoreload"),
        ({}, "manual"),
    ],
)
def test_restart_modes(monkeypatch, environment, expected):
    """Each supervisor gets its restart; exit uses the code that halts the Gunicorn master (#290)."""
    from skeleton.setup import restart

    for name in ("SKELETON_SETUP_RESTART", "SERVER_SOFTWARE", "RUN_MAIN"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    scheduled, calls = [], []
    monkeypatch.setattr(restart, "_delayed", lambda action, seconds=1.0: scheduled.append(action))
    monkeypatch.setattr(restart.os, "_exit", lambda code: calls.append(("exit", code)))
    monkeypatch.setattr(restart.os, "kill", lambda pid, sig: calls.append(("kill", sig)))
    monkeypatch.setattr(restart.Path, "touch", lambda self: calls.append(("touch", self.name)))
    assert restart.request_restart() == expected
    assert restart.GUNICORN_HALT_EXIT_CODE == 3
    for action in scheduled:
        action()
    expected_calls = {
        "exit": [("exit", 3)],
        "gunicorn": [("kill", getattr(signal, "SIGHUP", None))],
        "autoreload": [("touch", "restart.py")],
        "manual": [],
    }[expected]
    assert calls == expected_calls
