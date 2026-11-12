"""Personal data is redacted in the audit (#189); old events are redacted once by the owner."""

import os

import psycopg
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from psycopg import sql

from skeleton.accounts.services import create_identity, update_profile
from skeleton.tenancy.context import request_context, system_context
from tools.integration_database import disposable_database_pair

pytestmark = pytest.mark.usefixtures("skeleton_db")
PASSWORD = "Personal-data-audit-2026!418"


def events(table, record_id=None):
    """Audit rows read as the owner (the global audit is hidden from the runtime role by RLS)."""
    query = "SELECT sysid, changes FROM skeleton.sk_audit_auditevent WHERE table_name = %s"
    params = [table]
    if record_id is not None:
        query += " AND record_sysid = %s"
        params.append(record_id)
    with psycopg.connect(disposable_database_pair().owner) as connection:
        return connection.execute(query + " ORDER BY record_revision", params).fetchall()


def test_new_events_redact_personal_fields_but_the_api_still_reads_them():
    with system_context(reason="test:personal-audit-create"):
        user = create_identity(email="pessoa@example.test", password=PASSWORD)
    with request_context(actor=user, request_id=None, source="test:personal-audit"):
        update_profile(user, data={"first_name": "Maria", "display_name": "Maria S."})
    rows = events("sk_accounts_user", user.pk)
    assert rows, "the identity must have audit events"
    seen = set()
    for _sysid, changes in rows:
        for name in ("email", "first_name", "last_name"):
            if name in changes:
                seen.add(name)
                assert changes[name]["redacted"] is True
                assert "pessoa@example.test" not in str(changes[name]) and "Maria" not in str(changes[name])
    assert {"email", "first_name"} <= seen
    profile = [changes for _sysid, changes in events("sk_accounts_userprofile") if "display_name" in changes]
    assert profile and all(changes["display_name"]["redacted"] for changes in profile)
    user.refresh_from_db()
    assert (user.email, user.first_name) == ("pessoa@example.test", "Maria")  # Readable as before.


def _plant_legacy_value(event_id):
    """Simulate an event written before #189: the owner puts a clear email back."""
    owner = disposable_database_pair().owner
    with psycopg.connect(owner) as connection, connection.transaction():
        table = sql.SQL("skeleton.sk_audit_auditevent")
        connection.execute(sql.SQL("ALTER TABLE {} DISABLE TRIGGER sk_audit_auditevent_sk_guard").format(table))
        connection.execute(
            sql.SQL(
                "UPDATE {} SET changes = jsonb_set(changes, '{{email}}', "
                '\'{{"old": null, "new": "legado@example.test", "redacted": false}}\'::jsonb) WHERE sysid = %s'
            ).format(table),
            [event_id],
        )
        connection.execute(sql.SQL("ALTER TABLE {} ENABLE TRIGGER sk_audit_auditevent_sk_guard").format(table))


def test_the_owner_redacts_old_events_once():
    with system_context(reason="test:personal-audit-legacy"):
        user = create_identity(email="legado@example.test", password=PASSWORD)
    created_id = events("sk_accounts_user", user.pk)[0][0]
    _plant_legacy_value(created_id)
    assert dict(events("sk_accounts_user", user.pk))[created_id]["email"]["new"] == "legado@example.test"
    owner = disposable_database_pair().owner
    call_command("redact_audit_personal_data", database_url=owner, confirm=True, verbosity=0)
    value = dict(events("sk_accounts_user", user.pk))[created_id]["email"]
    assert value == {"old": None, "new": "[REDACTED]", "redacted": True}
    # The guard is back: the audit is append-only again.
    with psycopg.connect(owner) as connection:
        enabled = connection.execute(
            "SELECT tgenabled FROM pg_trigger WHERE tgname = 'sk_audit_auditevent_sk_guard'"
        ).fetchone()[0]
        assert enabled == "O"
        with pytest.raises(psycopg.Error):
            connection.execute("UPDATE skeleton.sk_audit_auditevent SET source = 'x' WHERE sysid = %s", [created_id])


def test_the_runtime_role_and_a_missing_confirmation_are_refused():
    with pytest.raises(CommandError):
        call_command("redact_audit_personal_data", database_url=disposable_database_pair().owner, verbosity=0)
    with pytest.raises(CommandError):
        call_command(
            "redact_audit_personal_data", database_url=os.environ["TEST_DATABASE_URL"], confirm=True, verbosity=0
        )
