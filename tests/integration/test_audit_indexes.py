"""AuditEvent keeps only indexes that a query reads (#270)."""

import pytest
from django.db import connection

from skeleton.audit.models import AuditEvent

pytestmark = pytest.mark.usefixtures("skeleton_db")

EXPECTED = {
    "sk_audit_org_created": "(organization_id, created_at)",
    "sk_audit_record_created": "(record_sysid, created_at)",
    "sk_audit_org_actor": "(organization_id, actor_sysid, created_at)",  # Investigation filters (#184).
    "sk_audit_request": "(request_id)",
}


def test_audit_event_has_exactly_the_indexes_with_readers():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'skeleton' AND tablename = %s",
            [AuditEvent._meta.db_table],
        )
        indexes = dict(cursor.fetchall())
    primary = [name for name, definition in indexes.items() if name.endswith("_pkey")]
    assert len(primary) == 1 and indexes[primary[0]].endswith("(sysid)")
    assert set(indexes) - set(primary) == set(EXPECTED)
    for name, columns in EXPECTED.items():
        assert indexes[name].endswith(columns), indexes[name]
    assert "sk_audit_record_revision" not in indexes


def test_model_declares_no_single_column_audit_index():
    assert AuditEvent._meta.get_field("organization_id").db_index is False
    assert AuditEvent._meta.get_field("record_sysid").db_index is False
    assert {index.name for index in AuditEvent._meta.indexes} == set(EXPECTED)
