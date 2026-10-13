"""Isolated real databases without Django flush/TRUNCATE of protected records."""
import os
import sqlite3

import environ
import pytest
from django.conf import settings
from django.core.management import call_command
from django.db import connections
from django.db.utils import ConnectionHandler
from django.test import override_settings


def _configure(config):
    connections.close_all()
    normalized = ConnectionHandler({"default": config}).settings["default"]
    settings.DATABASES["default"] = normalized
    connections.databases["default"] = normalized
    connections["default"].settings_dict = normalized
    return normalized




@pytest.fixture(scope="session")
def sqlite_template(tmp_path_factory, django_db_blocker):
    path = tmp_path_factory.mktemp("schema") / "template.sqlite3"
    original = connections["default"].settings_dict.copy()
    with django_db_blocker.unblock():
        _configure({"ENGINE": "django.db.backends.sqlite3", "NAME": str(path)})
        call_command("migrate", verbosity=0, interactive=False)
        connections.close_all()
        _configure(original)
    return path


@pytest.fixture
def skeleton_db(request, tmp_path, django_db_blocker):
    """Real committed per-test DB; protected rows are never flushed/deleted."""
    original = connections["default"].settings_dict.copy()
    runtime_url = os.environ.get("TEST_DATABASE_URL", "")
    with django_db_blocker.unblock():
        if runtime_url.startswith(("postgres://", "postgresql://")):
            from tools.integration_database import disposable_database_pair, reset_schema
            try:
                pair = disposable_database_pair()
            except RuntimeError as exc:
                raise pytest.UsageError(str(exc)) from exc
            reset_schema(pair)
            runtime, owner = pair.runtime, pair.owner
            config = environ.Env.db_url_config(owner)
            config["OPTIONS"] = {"options": "-c search_path=skeleton,public"}
            _configure(config)
            call_command("migrate", verbosity=0, interactive=False)
            config = environ.Env.db_url_config(runtime)
            config["OPTIONS"] = {"options": "-c search_path=skeleton,public"}
            _configure(config)
        else:
            template = request.getfixturevalue("sqlite_template")
            target = tmp_path / "test.sqlite3"
            with sqlite3.connect(template) as source, sqlite3.connect(target) as dest:
                source.backup(dest)
            _configure({"ENGINE": "django.db.backends.sqlite3", "NAME": str(target)})
        try:
            # Do not clear a configured external cache or share rate limits across tests.
            with override_settings(CACHES={"default": {
                    "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                    "LOCATION": str(tmp_path),
            }}):
                yield
        finally:
            connections.close_all()
            _configure(original)
