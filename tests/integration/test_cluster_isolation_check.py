"""skeleton.W051 (#679): the runtime role should reach only the database of its own instance."""

import os

import psycopg
import pytest

from skeleton.system.checks.environment import other_databases

RUNTIME = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not RUNTIME, reason="TEST_DATABASE_URL is required.")


def test_the_runtime_role_sees_the_other_databases_it_may_connect_to():
    # The disposable test server keeps the default CONNECT of PUBLIC on its databases: the
    # situation the check exists for (several databases in one cluster).
    with psycopg.connect(RUNTIME, autocommit=True) as connection, connection.cursor() as cursor:
        reachable = other_databases(cursor)
        cursor.execute("SELECT current_database()")
        current = cursor.fetchone()[0]
    assert reachable and current not in reachable
    assert not any(name.startswith("template") for name in reachable)


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql):
        assert "has_database_privilege" in sql

    def fetchall(self):
        return self.rows


def test_an_isolated_cluster_has_nothing_to_report():
    assert other_databases(Cursor([])) == []
    assert other_databases(Cursor([("other_instance",)])) == ["other_instance"]
