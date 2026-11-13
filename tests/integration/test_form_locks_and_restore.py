"""Restore only tombstones; a form GET takes no row lock (#288)."""

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from tests.support import login
from tools.security_fixtures import actor_tenant, build_security_fixture

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]
TABLE = "example_notes.note"
PASSWORD = "Fixture-only-password-2026!"


@pytest.fixture
def records():
    from examples.notes.models import Note

    fixture = build_security_fixture()
    with actor_tenant(fixture.admin, fixture.a):
        fixture.deleted = Note(organization=fixture.a, owner=fixture.admin, code="LOCK-DEL", title="Deleted")
        fixture.deleted.save()
        fixture.deleted.delete()
        fixture.deleted.refresh_from_db()
    fixture.client = login(fixture.admin, password=PASSWORD, csrf=False)
    return fixture


def record_url(fixture, name, note):
    return reverse(name, kwargs={"organization_id": fixture.a.pk, "table_key": TABLE, "sysid": note.pk})


def api_restore(fixture, note):
    return reverse("api:restore", kwargs={"organization_id": fixture.a.pk, "table_key": TABLE, "sysid": note.pk})


def audit_count(note):
    from skeleton.audit.models import AuditEvent
    from skeleton.tenancy.context import system_context

    with system_context(reason="test:restore-audit", organization_id=note.organization_id):
        return AuditEvent.objects.filter(record_sysid=note.pk).count()


def test_restore_of_a_live_record_is_not_found(records, monkeypatch):
    from examples.notes.models import Note
    from skeleton.system import record_operations
    from skeleton.system.record_operations import RecordOperations, register_record_operations

    calls = []

    def restore(record, *, user):
        calls.append(record.pk)
        record.restore()

    restore.__module__ = Note.__module__
    monkeypatch.setattr(record_operations, "_operations", dict(record_operations._operations))
    monkeypatch.delitem(record_operations._operations, TABLE, raising=False)
    register_record_operations(TABLE, RecordOperations(restore=restore))
    live = records.private
    before = audit_count(live)
    url = record_url(records, "web:record-restore", live)
    assert records.client.get(url).status_code == 404
    assert records.client.post(url, {"revision": live.revision}).status_code == 404
    response = records.client.post(
        api_restore(records, live), "{}", content_type="application/json", HTTP_IF_MATCH=f'"{live.revision}"'
    )
    assert response.status_code == 404
    assert audit_count(live) == before
    assert calls == []  # The business restore service never ran on a live record.


def test_restore_of_a_deleted_record_still_works(records):
    from examples.notes.models import Note

    deleted = records.deleted
    url = record_url(records, "web:record-restore", deleted)
    assert records.client.get(url).status_code == 200
    assert records.client.post(url, {"revision": deleted.revision}).status_code == 302
    with actor_tenant(records.admin, records.a):
        restored = Note.objects.get(pk=deleted.pk)
        assert restored.deleted_at is None
        restored.delete()
        restored.refresh_from_db()
    response = records.client.post(
        api_restore(records, restored), "{}", content_type="application/json", HTTP_IF_MATCH=f'"{restored.revision}"'
    )
    assert response.status_code == 200 and response.json()["sysid"] == str(deleted.pk)


def test_get_forms_take_no_row_lock(records):
    note, deleted = records.private, records.deleted
    access = {"organization_id": records.a.pk, "resource": "memberships", "sysid": records.bob_membership.pk}
    urls = [
        record_url(records, "web:record-edit", note),
        record_url(records, "web:record-delete", note),
        record_url(records, "web:record-access", note),
        record_url(records, "web:record-restore", deleted),
        reverse("web:access-edit", kwargs=access),
        reverse("web:access-delete", kwargs=access),
    ]
    for url in urls:
        with CaptureQueriesContext(connection) as captured:
            assert records.client.get(url).status_code == 200, url
        assert not [query["sql"] for query in captured if "FOR UPDATE" in query["sql"]], url


def test_post_forms_still_lock_the_row(records):
    """A stale POST still locks the row before comparing the revision, and answers 409."""
    note = records.private
    access = {"organization_id": records.a.pk, "resource": "memberships", "sysid": records.bob_membership.pk}
    for url, data in (
        (
            record_url(records, "web:record-edit", note),
            {"code": note.code, "title": "x", "revision": note.revision + 99},
        ),
        (reverse("web:access-edit", kwargs=access), {"revision": records.bob_membership.revision + 99}),
    ):
        with CaptureQueriesContext(connection) as captured:
            response = records.client.post(url, data)
        assert response.status_code == 409, url
        assert [query["sql"] for query in captured if "FOR UPDATE" in query["sql"]], url
