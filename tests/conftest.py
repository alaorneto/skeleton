"""Isolated real PostgreSQL databases without Django flush/TRUNCATE of protected records.

The whole suite runs on PostgreSQL (#145): migrations as ``skeleton_owner``, assertions as
``skeleton_runtime`` (NOSUPERUSER, NOBYPASSRLS). ``tools/dev_postgres.py start`` provides a
disposable local server; ``eval "$(tools/dev_postgres.py env)"`` exports the variables.

With ``pytest -n`` (pytest-xdist) every worker process uses a database of its own,
``<prefix>_gwN_skeleton_test``, provisioned beforehand like the base one. The fixtures never
create a database or a role: a missing worker database stops the run with a usage error.
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


def pytest_addoption(parser):
    parser.addoption(
        "--shard",
        metavar="I/N",
        default=None,
        help="Run only slice I of N of the test files (tools/suite_shards.py); the N slices are the whole suite.",
    )


def pytest_configure(config):
    if config.getoption("shard"):
        from tools.suite_shards import parse_shard

        try:
            parse_shard(config.getoption("shard"))
        except ValueError as exc:
            raise pytest.UsageError(str(exc)) from exc
    if hasattr(config, "workerinput"):
        _select_worker_database()
    elif getattr(config.option, "numprocesses", None) and not config.option.collectonly:
        _require_worker_databases(config.option.numprocesses)


def _select_worker_database():
    """In a pytest-xdist worker: this process only ever sees its own database."""
    from tools.integration_database import RUNTIME_ENV, select_worker_database

    if select_worker_database() is not None and settings.DATABASES.get("default"):
        # The settings were loaded from the base URL before this hook ran.
        _configure_postgres(os.environ[RUNTIME_ENV])


def _require_worker_databases(count):
    """In the controller: fail once, before any worker starts, when a database is missing."""
    from tools.integration_database import (
        OWNER_ENV,
        RUNTIME_ENV,
        lock_table_shortfall,
        missing_worker_databases,
    )

    if not os.environ.get(RUNTIME_ENV) or not os.environ.get(OWNER_ENV):
        return  # Tests without a database still run; the others report the missing variables.
    try:
        missing = missing_worker_databases(count)
        shortfall = None if missing else lock_table_shortfall(count)
    except RuntimeError as exc:
        raise pytest.UsageError(str(exc)) from exc
    if shortfall:
        raise pytest.UsageError(f"pytest -n {count}: {shortfall}")
    if missing:
        raise pytest.UsageError(
            f"pytest -n {count} needs one disposable database per worker, and {len(missing)} cannot be "
            f"opened by skeleton_owner (first: {missing[0]}). Provision them with "
            f"`tools/dev_postgres.py start --workers {count}` (in CI, the provisioning step), check "
            "that the server is running, or lower -n. The fixtures never create databases."
        )


def _keep_shard(config, items):
    """--shard I/N: keep the files of one slice; the other slices run them, none is skipped."""
    from tools.suite_shards import parse_shard, split_shard

    index, total = parse_shard(config.getoption("shard"))
    selected, _ = split_shard([item.nodeid for item in items], index, total)
    keep = set(selected)
    deselected = [item for item in items if item.nodeid not in keep]
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = [item for item in items if item.nodeid in keep]


def pytest_collection_modifyitems(config, items):
    """Production artifact handling is POSIX-only; report it as a skip, never as an error."""
    if config.getoption("shard"):
        _keep_shard(config, items)
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
    return {"migrate": 0, "cached": 0, "restore": 0}


def _template_cache_key(pair):
    from tools.integration_database import pg_dump_version, server_version, template_cache_key

    return template_cache_key(
        server_version=server_version(pair),
        pg_dump_version=pg_dump_version(),
        installed_apps=settings.INSTALLED_APPS,
        migration_modules=getattr(settings, "MIGRATION_MODULES", {}),
    )


@pytest.fixture(scope="session")
def postgres_template(django_db_blocker, postgres_schema_stats):
    """Migrate once per session and keep the migrated schema skeleton as a restorable template.

    The template of a previous run is reused when nothing that shapes the schema changed
    (tools/integration_database.py, #350); SKELETON_TEST_TEMPLATE_CACHE=0 always migrates.
    Returns None when SKELETON_TEST_POSTGRES_TEMPLATE=0: skeleton_db then migrates for each test.
    """
    from tools.integration_database import (
        dump_schema,
        load_cached_template,
        save_cached_template,
        template_cache_dir,
        template_enabled,
    )

    if not template_enabled():
        return None
    pair = _disposable_pair()
    directory = template_cache_dir()
    key = _template_cache_key(pair) if directory is not None else None
    cached = load_cached_template(directory, key)
    if cached is not None:
        postgres_schema_stats["cached"] += 1
        return cached
    original = connections["default"].settings_dict.copy()
    with django_db_blocker.unblock():
        try:
            _migrate_postgres(pair, postgres_schema_stats)
        finally:
            _configure(original)
        try:
            template = dump_schema(pair)
        except RuntimeError as exc:
            raise pytest.UsageError(str(exc)) from exc
    save_cached_template(directory, key, template)
    return template


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
