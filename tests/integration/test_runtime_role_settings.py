"""The runtime role has bounded lock and idle-transaction waits; the owner does not (#318).

Both come from ``deploy/postgres/bootstrap.sql`` (``ALTER ROLE ... SET``), which also provisions the
test databases. A local cluster created before #318 needs the two ``ALTER ROLE`` statements of
docs/deployment.md, "Provisionamento PostgreSQL", or a fresh ``tools/dev_postgres.py start``.
"""

import os

import psycopg
import pytest
from django.db import connection

pytestmark = pytest.mark.usefixtures("skeleton_db")
SETTINGS = "SELECT name, setting FROM pg_settings WHERE name IN ('lock_timeout', 'idle_in_transaction_session_timeout')"


def test_runtime_role_has_bounded_lock_and_idle_waits():
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        assert cursor.fetchone() == ("skeleton_runtime",)
        cursor.execute(SETTINGS)
        assert dict(cursor.fetchall()) == {"lock_timeout": "10000", "idle_in_transaction_session_timeout": "60000"}


def test_owner_role_is_unbounded():
    # The owner only reads its own session configuration here; it proves nothing about RLS.
    with psycopg.connect(os.environ["TEST_MIGRATION_DATABASE_URL"]) as owner:
        assert owner.execute("SELECT current_user").fetchone() == ("skeleton_owner",)
        assert dict(owner.execute(SETTINGS).fetchall()) == {
            "lock_timeout": "0",
            "idle_in_transaction_session_timeout": "0",
        }
