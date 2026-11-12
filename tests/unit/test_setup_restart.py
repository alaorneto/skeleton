"""request_restart() picks how to restart after the setup, without ever running the action (#335)."""

import os
import signal

import pytest

from skeleton.setup import restart


@pytest.fixture
def captured(monkeypatch):
    actions = []
    monkeypatch.setattr(restart, "_delayed", lambda action, seconds=1.0: actions.append(action))
    for name in ("SKELETON_SETUP_RESTART", "SERVER_SOFTWARE", "RUN_MAIN"):
        monkeypatch.delenv(name, raising=False)
    return actions


def test_exit_mode_schedules_process_exit(monkeypatch, captured):
    calls = []
    monkeypatch.setenv("SKELETON_SETUP_RESTART", "exit")
    assert restart.request_restart() == "exit"
    monkeypatch.setattr(os, "_exit", calls.append)
    captured[0]()
    assert calls == [3]


@pytest.mark.posix
def test_gunicorn_mode_signals_the_parent(monkeypatch, captured):
    calls = []
    monkeypatch.setenv("SERVER_SOFTWARE", "gunicorn/23")
    assert restart.request_restart() == "gunicorn"
    monkeypatch.setattr(os, "kill", lambda pid, sig: calls.append((pid, sig)))
    captured[0]()
    assert calls == [(os.getppid(), signal.SIGHUP)]


def test_autoreload_mode_touches_the_module(monkeypatch, captured):
    touched = []
    monkeypatch.setenv("RUN_MAIN", "true")
    assert restart.request_restart() == "autoreload"
    monkeypatch.setattr("pathlib.Path.touch", lambda self, *args, **kwargs: touched.append(self))
    captured[0]()
    assert [path.name for path in touched] == ["restart.py"]


def test_manual_mode_schedules_nothing(captured):
    assert restart.request_restart() == "manual"
    assert captured == []


def test_exit_mode_wins_over_gunicorn(monkeypatch, captured):
    monkeypatch.setenv("SKELETON_SETUP_RESTART", "EXIT")
    monkeypatch.setenv("SERVER_SOFTWARE", "gunicorn/23")
    assert restart.request_restart() == "exit"
    assert len(captured) == 1
