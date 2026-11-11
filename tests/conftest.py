"""Isolated real PostgreSQL databases without Django flush/TRUNCATE of protected records.

The whole suite runs on PostgreSQL (#145): migrations as ``skeleton_owner``, assertions as
``skeleton_runtime`` (NOSUPERUSER, NOBYPASSRLS). ``tools/dev_postgres.py start`` provides a
disposable local server; ``eval "$(tools/dev_postgres.py env)"`` exports the variables.
"""

import os
from contextlib import contextmanager

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
# The real SSO protocol suite needs Keycloak, a browser and tools/prepare_sso_fixtures.py; it is
# collected only when its fixture environment is configured (docs/testing.md).
collect_ignore = [] if os.environ.get("SSO_FIXTURE_ENV_PATH") else ["sso"]


def pytest_collection_modifyitems(config, items):
    """Production artifact handling is POSIX-only; report it as a skip, never as an error."""
    if POSIX_FILESYSTEM:
        return
    skip = pytest.mark.skip(reason="Requer os.O_NOFOLLOW/fcntl; execute em Linux ou macOS.")
    for item in items:
        if item.get_closest_marker("posix"):
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def api_contract(request):
    """Every /api/v1/ response a test receives must match the published OpenAPI schema (#179).

    ``@pytest.mark.no_api_contract`` opts a test out, for one that builds a divergent response
    on purpose.
    """
    if request.node.get_closest_marker("no_api_contract"):
        yield
        return
    from skeleton.testing.openapi import checked_client_responses

    with checked_client_responses():
        yield


def _postgres_config(dsn):
    config = environ.Env.db_url_config(dsn)
    config["OPTIONS"] = {"options": "-c search_path=skeleton,public"}
    return config


def _configure_postgres(dsn):
    _configure(_postgres_config(dsn))


def _disposable_pair():
    from tools.integration_database import disposable_database_pair

    try:
        return disposable_database_pair()
    except RuntimeError as exc:
        raise pytest.UsageError(
            f"{exc} The suite runs on PostgreSQL only: start one with `tools/dev_postgres.py start` "
            "and export `tools/dev_postgres.py env`."
        ) from exc


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

    if not template_enabled():
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
    from django.contrib.contenttypes.models import ContentType

    from tools.integration_database import restore_schema

    original = connections["default"].settings_dict.copy()
    with django_db_blocker.unblock():
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
                if request.node.get_closest_marker("closed_app_defaults"):
                    yield
                else:
                    # Precondition of most tests: apps open and active, as an upgraded deployment
                    # keeps them (#22). Tests of the closed default opt out with the marker.
                    from skeleton.testing.applications import apps_open_by_default

                    with apps_open_by_default():
                        yield
        finally:
            connections.close_all()
            _configure(original)


@pytest.fixture
def owner_schema_editor(skeleton_db):
    """DDL of a throwaway test table as ``skeleton_owner``, the role that migrates.

    Only setup DDL runs as the owner; every assertion keeps the runtime connection. The next
    test restores the schema from the template, so the table never outlives its test.
    """
    pair = _disposable_pair()

    @contextmanager
    def editor():
        alias = "test_schema_owner"
        connections.databases[alias] = ConnectionHandler({"default": _postgres_config(pair.owner)}).settings["default"]
        try:
            with connections[alias].schema_editor() as schema_editor:
                yield schema_editor
        finally:
            connections[alias].close()
            del connections[alias]
            del connections.databases[alias]

    return editor
