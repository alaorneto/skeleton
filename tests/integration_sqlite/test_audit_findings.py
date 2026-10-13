from dataclasses import replace
import pytest
from django.test import Client
from django.test.utils import isolate_apps
from skeleton.system import registry
from skeleton.system.registry import TableSpec
from tools.security_fixtures import build_security_fixture, actor_tenant

pytestmark = pytest.mark.usefixtures('skeleton_db')


def login(user):
    client = Client(raise_request_exception=False)
    assert client.post('/accounts/login/', {'login': user.email, 'password': 'Fixture-only-password-2026!'}).status_code == 302
    return client


@isolate_apps()
def test_sbr05_audit_only_table_does_not_abort_permitted_other_table(monkeypatch):
    from examples.notes.models import Note
    from skeleton.system.models import TableDefinition
    from skeleton.access.models import TablePermission
    fixture = build_security_fixture()
    class AuditOnlyNote(Note):
        class Meta:
            proxy = True
            app_label = 'example_notes'
    spec = TableSpec(model=AuditOnlyNote, description='Audit-only projection', read_fields=('code',), expose_api=True)
    monkeypatch.setitem(registry._registry, AuditOnlyNote._meta.label_lower, spec)
    with actor_tenant(fixture.admin, fixture.a):
        table = TableDefinition(database_alias='default', schema_name='projection_fixture', db_table=Note._meta.db_table,
                                model_label=AuditOnlyNote._meta.label_lower, label='Projection', origin='application', scope='organization')
        table.save()
        TablePermission(organization=fixture.a, role=fixture.member_role, table=table, action='audit').save()
    response = login(fixture.alice).get(f'/api/v1/o/{fixture.a.pk}/audit/')
    assert response.status_code == 200
    events = response.json()['results']
    assert any(event['record_sysid'] == str(fixture.private.pk) for event in events)
    assert all(event['model_label'] != AuditOnlyNote._meta.label_lower for event in events)


def test_sbr06_audit_representation_excludes_unreadable_fields_but_keeps_capture(monkeypatch):
    from examples.notes.models import Note
    from skeleton.audit.models import AuditEvent
    fixture = build_security_fixture()
    original = registry.get_spec(Note._meta.label_lower)
    monkeypatch.setitem(registry._registry, Note._meta.label_lower, replace(original, read_fields=('code', 'body')))
    with actor_tenant(fixture.alice, fixture.a):
        note = Note.objects.get(pk=fixture.private.pk)
        note.title = 'hidden-audit-sentinel'
        note.save(update_fields=['title'])
        event = AuditEvent.objects.get(record_sysid=note.pk, record_revision=note.revision)
        assert event.changes['title']['new'] == 'hidden-audit-sentinel'
    client = login(fixture.alice)
    response = client.get(f'/api/v1/o/{fixture.a.pk}/audit/{event.pk}/')
    assert response.status_code == 200
    assert 'title' not in response.json()['changes']
    assert b'hidden-audit-sentinel' not in response.content
    html = client.get(f'/o/{fixture.a.pk}/audit/', {'record_sysid': str(note.pk)})
    assert html.status_code == 200
    assert b'hidden-audit-sentinel' not in html.content
