"""Public record APIs and actual SQLite SQL, not source-string assertions."""
import json
import sqlite3
from uuid import UUID, uuid4

import pytest
from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models import UUIDField

from skeleton.common.exceptions import ConcurrentUpdate, ImmutableRecord, UnsafeBulkMutation
from skeleton.common.models import SystemRecord
from tools.security_fixtures import actor_tenant, build_security_fixture


@pytest.fixture
def records(skeleton_db):
    assert connection.vendor == 'sqlite', 'Use the SQLite fixture for this suite.'
    return build_security_fixture()


def events(record):
    from skeleton.audit.models import AuditEvent
    return list(AuditEvent.objects.filter(record_sysid=record.pk).order_by('record_revision'))


def test_all_owned_models_are_uuid_and_physically_catalogued(records):
    from skeleton.system.models import TableDefinition
    from skeleton.system.registry import get_spec
    for model in apps.get_models():
        if issubclass(model, SystemRecord):
            assert isinstance(model._meta.pk, UUIDField)
            assert model._meta.pk.name == 'sysid'
            assert get_spec(model._meta.label_lower).model is model
    physical = set(connection.introspection.table_names()) - {'sqlite_sequence'}
    catalog = set(TableDefinition.objects.filter(available=True).values_list('db_table', flat=True))
    assert physical <= catalog


def test_cas_has_one_winner_and_empty_update_fields_is_noop(records):
    from examples.notes.models import Note
    with actor_tenant(records.alice, records.a):
        first = Note.objects.get(pk=records.private.pk)
        second = Note.objects.get(pk=first.pk)
        count = len(events(first))
        first.title = 'winner'
        first.body = 'unsaved in-memory change'
        first.save(update_fields=['title'])
        second.title = 'loser'
        with pytest.raises(ConcurrentUpdate):
            second.save(update_fields=['title'])
        stored = Note.objects.get(pk=first.pk)
        assert stored.title == 'winner'
        assert stored.body == 'secret body private'
        assert stored.revision == 2
        assert len(events(first)) == count + 1
        stored.save(update_fields=[])
        assert len(events(stored)) == count + 1
        assert stored.revision == 2
        assert stored.updated_by == records.alice.pk


def test_uuid_and_organization_are_immutable_without_upsert(records):
    from examples.notes.models import Note
    with actor_tenant(records.alice, records.a):
        note = Note.objects.get(pk=records.private.pk)
        original = note.pk
        note.sysid = uuid4()
        with pytest.raises(ImmutableRecord):
            note.save()
        assert Note.objects.get(pk=original).title == 'Private'
        note = Note.objects.get(pk=original)
        note.organization = records.b
        with pytest.raises(ImmutableRecord):
            note.save()
        collision = Note(sysid=original, organization=records.a, owner=records.alice, code='different', title='No overwrite', body='')
        with pytest.raises((ValidationError, IntegrityError)):
            collision.save()
        assert Note.objects.get(pk=original).title == 'Private'


def test_queryset_bulk_sql_soft_delete_restore_and_redaction(records):
    from examples.notes.models import Note
    with actor_tenant(records.alice, records.a):
        notes = [Note(organization=records.a, owner=records.alice, code=f'bulk-{i}', title='Bulk', body=f'never-store-secret-{i}') for i in range(2)]
        Note.objects.bulk_create(notes)
        for note in notes:
            assert isinstance(note.pk, UUID)
            assert len(events(note)) == 1
            assert events(note)[0].actor_sysid == records.alice.pk
            assert events(note)[0].changes['title'] == {'old': None, 'new': 'Bulk', 'redacted': False}
            assert events(note)[0].changes['owning_group_id'] == {'old': None, 'new': None, 'redacted': False}
        with pytest.raises(UnsafeBulkMutation):
            Note.objects.bulk_update(notes, ['title'])
        with pytest.raises(UnsafeBulkMutation):
            Note.objects.bulk_create([], ignore_conflicts=True)
        with pytest.raises(UnsafeBulkMutation):
            Note.objects.bulk_create([], update_conflicts=True, update_fields=['title'], unique_fields=['sysid'])
        Note.objects.filter(pk__in=[n.pk for n in notes]).update(title='Bulk update', body='changed secret')
        for note in notes:
            note.refresh_from_db()
            assert note.revision == 2
            assert len(events(note)) == 2
        # Raw UPDATE is audited by the database, not a save signal.
        quoted = connection.ops.quote_name(Note._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute(f'UPDATE {quoted} SET title=%s,revision=revision+1 WHERE sysid=%s', ['Direct SQL', notes[0].pk.hex])
        notes[0].refresh_from_db()
        assert len(events(notes[0])) == 3
        Note.objects.filter(pk__in=[n.pk for n in notes]).delete()
        assert not Note.objects.filter(pk__in=[n.pk for n in notes]).exists()
        for note in notes:
            tombstone = Note.objects.with_deleted().get(pk=note.pk)
            assert events(tombstone)[-1].operation == 'soft_delete'
            tombstone.restore()
            assert events(tombstone)[-1].operation == 'restore'
            restored = events(tombstone)[-1].changes['deleted_at']
            assert restored['old'] is not None and restored['new'] is None and restored['redacted'] is False
            assert tombstone.pk == note.pk
            serialized = json.dumps([event.changes for event in events(tombstone)])
            assert 'never-store-secret' not in serialized
            assert 'changed secret' not in serialized
            assert '[REDACTED]' in serialized
            for event in events(tombstone):
                assert event.organization_id == records.a.pk
                assert event.request_id is not None
                assert event.source == 'test:security'
                assert not {'revision','updated_at','created_at','updated_by','created_by'} & set(event.changes)


def test_restore_conflict_preserves_tombstone_and_replacement(records):
    from examples.notes.models import Note
    with actor_tenant(records.alice, records.a):
        original = Note.objects.get(pk=records.private.pk)
        original.delete()
        replacement = Note(organization=records.a, owner=records.alice, code=original.code, title='Replacement', body='')
        replacement.save()
        with pytest.raises((ValidationError, IntegrityError)):
            original.restore()
        original.refresh_from_db()
        assert original.deleted_at is not None
        assert Note.objects.get(pk=replacement.pk).title == 'Replacement'


def test_transaction_savepoint_rollbacks_remove_records_and_events(records):
    from examples.notes.models import Note
    with actor_tenant(records.alice, records.a):
        original = Note.objects.get(pk=records.private.pk)
        initial = len(events(original))
        with pytest.raises(RuntimeError):
            with transaction.atomic():
                original.title = 'Rolled back'
                original.save(update_fields=['title'])
                raise RuntimeError('rollback fixture')
        assert Note.objects.get(pk=original.pk).title == 'Private'
        assert len(events(original)) == initial


def test_actual_guards_reject_cross_tenant_fk_identity_and_audit_mutation(records):
    from examples.notes.models import Note
    from skeleton.audit.models import AuditEvent
    with actor_tenant(records.alice, records.a):
        table = connection.ops.quote_name(Note._meta.db_table)
        attempts = [(f'UPDATE {table} SET owning_group_id=%s,visibility=%s,revision=revision+1 WHERE sysid=%s', [records.foreign_group.pk.hex,'group',records.private.pk.hex]),
            (f'UPDATE {table} SET organization_id=%s,revision=revision+1 WHERE sysid=%s', [records.b.pk.hex,records.private.pk.hex]),
            (f'UPDATE {table} SET title=%s WHERE sysid=%s', ['No revision',records.private.pk.hex]),
            (f'DELETE FROM {table} WHERE sysid=%s', [records.private.pk.hex])]
        for statement, params in attempts:
            with pytest.raises(IntegrityError), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(statement, params)
        audit = connection.ops.quote_name(AuditEvent._meta.db_table)
        for statement in (f'UPDATE {audit} SET source=%s WHERE record_sysid=%s', f'DELETE FROM {audit} WHERE record_sysid=%s'):
            params = ['tamper',records.private.pk.hex] if statement.startswith('UPDATE') else [records.private.pk.hex]
            with pytest.raises(IntegrityError), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(statement, params)
        assert Note.objects.get(pk=records.private.pk).revision == 1


def test_external_sqlite_without_registered_udfs_fails_closed(records):
    from examples.notes.models import Note
    # A genuine second driver connection lacks Django's context/audit UDFs.
    with sqlite3.connect(connection.settings_dict['NAME']) as external:
        with pytest.raises(sqlite3.OperationalError, match='no such function'):
            external.execute(f'UPDATE "{Note._meta.db_table}" SET title=?,revision=revision+1 WHERE sysid=?', ('external',records.private.pk.hex))
    with actor_tenant(records.alice, records.a):
        assert Note.objects.get(pk=records.private.pk).title == 'Private'


def test_password_changes_are_detected_before_redaction(records):
    from skeleton.accounts.services import set_local_password
    from skeleton.tenancy.context import request_context
    with request_context(actor=records.alice, request_id=uuid4(), source='test:password'):
        before = len(events(records.alice))
        set_local_password(records.alice, 'Another-fixture-password-2026!')
        history = events(records.alice)
        assert len(history) == before + 1
        change = history[-1].changes['password']
        assert change['old'] == change['new'] == '[REDACTED]'
        assert change['redacted'] is True
        assert 'pbkdf2' not in json.dumps(history[-1].changes)


@pytest.mark.parametrize('invalid_sysid', ['not-a-guid', 'g' * 32])
def test_direct_sql_cannot_insert_non_guid_identity(records, invalid_sysid):
    from django.utils import timezone
    from examples.notes.models import Note
    with actor_tenant(records.alice, records.a):
        with pytest.raises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    'INSERT INTO example_notes_note '
                    '(sysid,created_at,updated_at,revision,code,title,body,organization_id,owner_id,visibility) '
                    'VALUES (%s,%s,%s,1,%s,%s,%s,%s,%s,%s)',
                    [invalid_sysid, timezone.now(), timezone.now(), 'invalid-guid', 'Invalid', '',
                     records.a.pk.hex, records.alice.pk.hex, 'private'],
                )
        assert not Note.objects.filter(code='invalid-guid').exists()
