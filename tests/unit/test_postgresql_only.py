"""The data layer is PostgreSQL only (#144): E025 and explicit migration refusals."""

from types import SimpleNamespace

import pytest
from django.core.exceptions import ImproperlyConfigured

from skeleton.common.migration_operations import InstallRecordInfrastructure, require_postgresql
from skeleton.system.checks import postgresql_only_checks

POSTGRES = {"ENGINE": "django.db.backends.postgresql", "NAME": "x_skeleton_test"}


def test_e025_refuses_any_other_backend(settings):
    settings.DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": "db.sqlite3"}}
    [error] = postgresql_only_checks()
    assert error.id == "skeleton.E025" and "sqlite3" in error.msg
    settings.DATABASES = {"default": POSTGRES, "reports": {"ENGINE": "django.db.backends.mysql"}}
    assert [error.msg for error in postgresql_only_checks()] == [
        "DATABASES['reports'] uses 'django.db.backends.mysql': the data layer is PostgreSQL only."
    ]


def test_e025_requires_a_database_outside_the_setup(settings):
    settings.DATABASES = {}
    settings.SKELETON_SETUP_MODE = False
    assert [error.id for error in postgresql_only_checks()] == ["skeleton.E025"]
    settings.SKELETON_SETUP_MODE = True
    assert postgresql_only_checks() == []
    settings.DATABASES = {"default": POSTGRES}
    settings.SKELETON_SETUP_MODE = False
    assert postgresql_only_checks() == []


def test_record_infrastructure_fails_explicitly_outside_postgresql():
    sqlite = SimpleNamespace(vendor="sqlite", alias="default")
    with pytest.raises(ImproperlyConfigured, match="requires PostgreSQL"):
        require_postgresql(sqlite)
    operation = InstallRecordInfrastructure("example_notes", "note", {"scope": "organization", "columns": []})
    editor = SimpleNamespace(connection=sqlite)
    state = SimpleNamespace(apps=SimpleNamespace(get_model=lambda *args: SimpleNamespace(_meta=None)))
    operation.allow_migrate_model = lambda *args: True
    with pytest.raises(ImproperlyConfigured, match="requires PostgreSQL"):
        operation.database_forwards("example_notes", editor, state, state)
    with pytest.raises(ImproperlyConfigured, match="requires PostgreSQL"):
        operation.database_backwards("example_notes", editor, state, state)
