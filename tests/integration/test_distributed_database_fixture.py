"""The distributed skeleton_db in a project outside this repository (#573): one migration, a restore per test.

A scratch project whose conftest only imports ``skeleton.testing.database.skeleton_db`` runs its
own pytest on the host settings of the wheel, against this worker's disposable database (the
subprocess inherits the selected worker database). The outer test holds ``skeleton_db`` so
nothing else uses the database meanwhile; the next test restores the core template.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

CONFTEST = """
from skeleton.testing.database import skeleton_db  # noqa: F401
"""
TESTS = """
import pytest

from skeleton.testing import database


def _fresh_then_write(email):
    from django.db import connection

    from skeleton.accounts.models import User
    from skeleton.accounts.services import create_identity
    from skeleton.tenancy.context import system_context

    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        assert cursor.fetchone() == ("skeleton_runtime",)
    with system_context(reason="test:distributed-fixture"):
        assert not User.objects.exists(), "the schema of the previous test survived"
        create_identity(email=email)


@pytest.mark.parametrize("index", range(3))
def test_each_test_starts_from_a_fresh_schema(skeleton_db, index):
    _fresh_then_write(f"person{index}@distributed.example.test")


def test_the_process_migrated_as_expected(skeleton_db):
    expected = (1, 4) if database.pg_dump_executable() and database.template_enabled() else (4, 0)
    assert (database.SCHEMA_STATS["migrate"], database.SCHEMA_STATS["restore"]) == expected
"""


def test_an_outside_project_migrates_once_and_restores_each_test(skeleton_db, tmp_path):
    from django.db import connections

    from skeleton.testing.database import disposable_database_pair

    pair = disposable_database_pair()
    (tmp_path / "conftest.py").write_text(textwrap.dedent(CONFTEST), encoding="utf-8")
    (tmp_path / "test_outside.py").write_text(textwrap.dedent(TESTS), encoding="utf-8")
    (tmp_path / "pytest.ini").write_text("[pytest]\nDJANGO_SETTINGS_MODULE = skeleton.testing.host.settings\n")
    environment = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "SKELETON_PROJECT_ROOT": str(tmp_path),
        "TEST_DATABASE_URL": pair.runtime,
        "TEST_MIGRATION_DATABASE_URL": pair.owner,
    }
    for name in ("DJANGO_SETTINGS_MODULE", "SKELETON_DEPLOYMENT_INVENTORY", "SKELETON_DEV_APPS"):
        environment.pop(name, None)
    connections.close_all()  # The subprocess recreates the schema: hold no connection to it.
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:xdist", str(tmp_path)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "4 passed" in completed.stdout
