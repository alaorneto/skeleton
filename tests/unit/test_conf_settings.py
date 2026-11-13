"""Settings of the core (#333): health checks of persistent connections and the test storage directory."""

import os
import subprocess
import sys
from pathlib import Path

import environ

from skeleton.conf.base import database_config

ROOT = Path(__file__).resolve().parents[2]
URL = "postgres://u:p@localhost:5432/db"


def test_database_config_enables_health_checks_for_persistent_connections(monkeypatch):
    monkeypatch.setenv("X_DATABASE_URL", URL)
    config = database_config(environ.Env(), "X_DATABASE_URL")
    assert config["CONN_MAX_AGE"] == 60 and config["CONN_HEALTH_CHECKS"] is True


def test_database_config_skips_health_checks_without_persistence(monkeypatch):
    monkeypatch.setenv("X_DATABASE_URL", URL)
    assert database_config(environ.Env(), "X_DATABASE_URL", conn_max_age=0)["CONN_HEALTH_CHECKS"] is False


def test_database_config_is_empty_without_the_variable(monkeypatch):
    monkeypatch.delenv("X_DATABASE_URL", raising=False)
    assert database_config(environ.Env(), "X_DATABASE_URL") is None


def test_test_settings_remove_their_storage_directory_at_exit():
    script = (
        "import django; django.setup(); from django.conf import settings; "
        "print(settings.STORAGES['skeleton_files']['OPTIONS']['location'])"
    )
    environment = {**os.environ, "DJANGO_SETTINGS_MODULE": "config.settings.test"}
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, env=environment, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr[-2000:]
    location = Path(result.stdout.strip().splitlines()[-1])
    assert location.name.startswith("skeleton-files-") and not location.exists()


def test_test_settings_add_no_unexpected_setting():
    import skeleton.conf.base as base
    from skeleton.conf.profiles import test_settings

    result = test_settings(dict(vars(base)))
    assert "FILES_ROOT" not in result and "files_root" not in result
    assert "STORAGES" in result
