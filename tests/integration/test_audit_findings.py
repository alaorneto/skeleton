import pytest
from django.test import Client
from django.test.utils import isolate_apps

from skeleton.system import registry
from skeleton.system.registry import TableSpec
from skeleton.testing.registries import table_spec_override
from tools.security_fixtures import actor_tenant, build_security_fixture

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]


def login(user):
    client = Client(raise_request_exception=False)
    assert (
        client.post("/accounts/login/", {"login": user.email, "password": "Fixture-only-password-2026!"}).status_code
        == 302
    )
    return client


@isolate_apps()
def test_sbr05_audit_only_table_does_not_abort_permitted_other_table(monkeypatch):
    from examples.notes.models import Note
    from skeleton.access.models import TablePermission
    from skeleton.system.models import TableDefinition

    fixture = build_security_fixture()

    class AuditOnlyNote(Note):
        class Meta:
            proxy = True
            app_label = "example_notes"

    spec = TableSpec(model=AuditOnlyNote, description="Audit-only projection", read_fields=("code",), expose_api=True)
    monkeypatch.setitem(registry._registry, AuditOnlyNote._meta.label_lower, spec)
    with actor_tenant(fixture.admin, fixture.a):
        table = TableDefinition(
            database_alias="default",
            schema_name="projection_fixture",
            db_table=Note._meta.db_table,
            model_label=AuditOnlyNote._meta.label_lower,
            label="Projection",
            origin="application",
            scope="organization",
        )
        table.save()
        TablePermission(organization=fixture.a, role=fixture.member_role, table=table, action="audit").save()
    response = login(fixture.alice).get(f"/api/v1/o/{fixture.a.pk}/audit/")
    assert response.status_code == 200
    events = response.json()["results"]
    assert any(event["record_sysid"] == str(fixture.private.pk) for event in events)
    assert all(event["model_label"] != AuditOnlyNote._meta.label_lower for event in events)


def test_sbr06_audit_representation_excludes_unreadable_fields_but_keeps_capture(monkeypatch):
    from examples.notes.models import Note
    from skeleton.audit.models import AuditEvent

    fixture = build_security_fixture()
    with table_spec_override(Note._meta.label_lower, read_fields=("code", "body")):
        with actor_tenant(fixture.alice, fixture.a):
            note = Note.objects.get(pk=fixture.private.pk)
            note.title = "hidden-audit-sentinel"
            note.save(update_fields=["title"])
            event = AuditEvent.objects.get(record_sysid=note.pk, record_revision=note.revision)
            assert event.changes["title"]["new"] == "hidden-audit-sentinel"
        client = login(fixture.alice)
        response = client.get(f"/api/v1/o/{fixture.a.pk}/audit/{event.pk}/")
        assert response.status_code == 200
        assert "title" not in response.json()["changes"]
        assert b"hidden-audit-sentinel" not in response.content
        html = client.get(f"/o/{fixture.a.pk}/audit/", {"record_sysid": str(note.pk)})
        assert html.status_code == 200
        assert b"hidden-audit-sentinel" not in html.content


def test_audit_page_shows_changes_as_a_table():
    """Field / Before / After instead of the repr of a Python dict (#287)."""
    from examples.notes.models import Note

    fixture = build_security_fixture()
    with actor_tenant(fixture.admin, fixture.a):
        note = Note.objects.get(pk=fixture.private.pk)
        note.title = "<b>x</b>"
        note.body = "segredo-do-corpo"
        note.save()
    response = login(fixture.admin).get(f"/o/{fixture.a.pk}/audit/?record_sysid={note.pk}")
    assert response.status_code == 200
    html = response.content.decode()
    assert "&#x27;old&#x27;" not in html and "None," not in html and "False}" not in html
    assert "<code>title</code>" in html and "&lt;b&gt;x&lt;/b&gt;" in html and "<b>x</b>" not in html
    assert "[redigido]" in html and "segredo-do-corpo" not in html


def test_platform_audit_page_uses_the_same_table():
    from skeleton.accounts.services import bootstrap_identity, update_profile
    from tests.support import login as login_with

    password = "Platform-audit-table-2026!"
    user, _ = bootstrap_identity(
        email="audit-table@example.test", organization="audit-table", name="Audit table", password=password
    )
    from skeleton.tenancy.context import request_context

    with request_context(actor=user, request_id=None, source="test:audit-table"):
        update_profile(user, data={"first_name": "Tabela"})
    html = login_with(user, password=password).get("/platform/audit/").content.decode()
    assert "<code>first_name</code>" in html and "Tabela" in html
    assert "&#x27;old&#x27;" not in html
