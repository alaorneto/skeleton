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


POSIX_FILESYSTEM = hasattr(os, "O_NOFOLLOW")


def pytest_collection_modifyitems(config, items):
    """Production artifact handling is POSIX-only; report it as a skip, never as an error."""
    if POSIX_FILESYSTEM:
        return
    skip = pytest.mark.skip(reason="Requer os.O_NOFOLLOW/fcntl; execute em Linux ou macOS.")
    for item in items:
        if item.get_closest_marker("posix"):
            item.add_marker(skip)


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


def _postgres_requested():
    return os.environ.get("TEST_DATABASE_URL", "").startswith(("postgres://", "postgresql://"))


def _configure_postgres(dsn):
    config = environ.Env.db_url_config(dsn)
    config["OPTIONS"] = {"options": "-c search_path=skeleton,public"}
    _configure(config)


def _disposable_pair():
    from tools.integration_database import disposable_database_pair

    try:
        return disposable_database_pair()
    except RuntimeError as exc:
        raise pytest.UsageError(str(exc)) from exc


def _migrate_postgres(pair, stats):
    """Fresh schema skeleton migrated by skeleton_owner; the caller restores the connection."""
    from tools.integration_database import reset_schema

    reset_schema(pair)
    _configure_postgres(pair.owner)
    call_command("migrate", verbosity=0, interactive=False)
    connections.close_all()
    stats["migrate"] += 1


@pytest.fixture(scope="session")
def postgres_schema_stats():
    """How the PostgreSQL schemas of this session were built (asserted by the template tests)."""
    return {"migrate": 0, "restore": 0}


@pytest.fixture(scope="session")
def postgres_template(django_db_blocker, postgres_schema_stats):
    """Migrate once per session and keep the migrated schema skeleton as a restorable template.

    Returns None when SKELETON_TEST_POSTGRES_TEMPLATE=0: skeleton_db then migrates for each test.
    """
    from tools.integration_database import dump_schema, template_enabled

    if not _postgres_requested() or not template_enabled():
        return None
    pair = _disposable_pair()
    original = connections["default"].settings_dict.copy()
    with django_db_blocker.unblock():
        try:
            _migrate_postgres(pair, postgres_schema_stats)
        finally:
            _configure(original)
        try:
            return dump_schema(pair)
        except RuntimeError as exc:
            raise pytest.UsageError(str(exc)) from exc


@pytest.fixture
def skeleton_db(request, tmp_path, django_db_blocker):
    """Real committed per-test DB; protected rows are never flushed/deleted."""
    original = connections["default"].settings_dict.copy()
    with django_db_blocker.unblock():
        if _postgres_requested():
            from django.contrib.contenttypes.models import ContentType

            from tools.integration_database import restore_schema

            pair = _disposable_pair()
            template = request.getfixturevalue("postgres_template")
            stats = request.getfixturevalue("postgres_schema_stats")
            if template is None:
                _migrate_postgres(pair, stats)
            else:
                # Same starting point as migrate: a recreated schema, never a flushed one.
                restore_schema(pair, template)
                stats["restore"] += 1
            # migrate's post_migrate handlers clear this cache; the restore does not run them.
            ContentType.objects.clear_cache()
            _configure_postgres(pair.runtime)
        else:
            template = request.getfixturevalue("sqlite_template")
            target = tmp_path / "test.sqlite3"
            with sqlite3.connect(template) as source, sqlite3.connect(target) as dest:
                source.backup(dest)
            _configure({"ENGINE": "django.db.backends.sqlite3", "NAME": str(target)})
        try:
            # Do not clear a configured external cache or share rate limits across tests.
            with override_settings(
                CACHES={
                    "default": {
                        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                        "LOCATION": str(tmp_path),
                    }
                }
            ):
                yield
        finally:
            connections.close_all()
            _configure(original)
