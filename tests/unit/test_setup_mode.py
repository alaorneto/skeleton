"""Setup mode choice, single-use token and DSN parsing, without a database (#146)."""

import io
import os
import signal
from datetime import UTC, datetime, timedelta

import pytest

from skeleton.instance.store import InstanceStore
from skeleton.setup import database, mode, tokens


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
    for name in ("DJANGO_SECRET_KEY", "DJANGO_ALLOWED_HOSTS", "DATABASE_URL", "SKELETON_SETUP"):
        monkeypatch.delenv(name, raising=False)
    assert mode.select_settings_module("config.settings.production", base_dir=tmp_path) == mode.SETUP_SETTINGS
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "config.settings.test")
    assert mode.select_settings_module("config.settings.production", base_dir=tmp_path) == "config.settings.test"


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
