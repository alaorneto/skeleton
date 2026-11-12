"""Public record APIs and actual PostgreSQL SQL (runtime role), not source-string assertions."""

import json
from uuid import UUID, uuid4

import pytest
from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.db.models import UUIDField

from skeleton.common.exceptions import ConcurrentUpdate, ImmutableRecord, UnsafeBulkMutation
from skeleton.common.models import SystemRecord
from tools.security_fixtures import actor_tenant, build_security_fixture

pytestmark = pytest.mark.needs_example_app


@pytest.fixture
def records(skeleton_db):
    assert connection.vendor == "postgresql", "The record contract runs on PostgreSQL with the runtime role."
    return build_security_fixture()


def events(record):
    from skeleton.audit.models import AuditEvent

    return list(AuditEvent.objects.filter(record_sysid=record.pk).order_by("record_revision"))


def test_all_owned_models_are_uuid_and_physically_catalogued(records):
    from skeleton.system.models import TableDefinition
    from skeleton.system.registry import get_spec

    for model in apps.get_models():
        if issubclass(model, SystemRecord):
            assert isinstance(model._meta.pk, UUIDField)
            assert model._meta.pk.name == "sysid"
            assert get_spec(model._meta.label_lower).model is model
    from skeleton.system.synchronization import physical_tables

    physical = set(physical_tables(connection))
    catalog = set(TableDefinition.objects.filter(available=True).values_list("db_table", flat=True))
    assert physical <= catalog


def test_cas_has_one_winner_and_empty_update_fields_is_noop(records):
    from examples.notes.models import Note

    with actor_tenant(records.alice, records.a):
        first = Note.objects.get(pk=records.private.pk)
        second = Note.objects.get(pk=first.pk)
        count = len(events(first))
        first.title = "winner"
        first.body = "unsaved in-memory change"
        first.save(update_fields=["title"])
        second.title = "loser"
        with pytest.raises(ConcurrentUpdate):
            second.save(update_fields=["title"])
        stored = Note.objects.get(pk=first.pk)
        assert stored.title == "winner"
        assert stored.body == "secret body private"
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
        assert Note.objects.get(pk=original).title == "Private"
        note = Note.objects.get(pk=original)
        note.organization = records.b
        with pytest.raises(ImmutableRecord):
            note.save()
        collision = Note(
            sysid=original, organization=records.a, owner=records.alice, code="different", title="No overwrite", body=""
        )
        with pytest.raises((ValidationError, IntegrityError)):
            collision.save()
        assert Note.objects.get(pk=original).title == "Private"


def test_queryset_bulk_sql_soft_delete_restore_and_redaction(records):
    from examples.notes.models import Note

    with actor_tenant(records.alice, records.a):
        notes = [
            Note(
                organization=records.a,
                owner=records.alice,
                code=f"bulk-{i}",
                title="Bulk",
                body=f"never-store-secret-{i}",
            )
            for i in range(2)
        ]
        Note.objects.bulk_create(notes)
        for note in notes:
            assert isinstance(note.pk, UUID)
            assert len(events(note)) == 1
            assert events(note)[0].actor_sysid == records.alice.pk
            assert events(note)[0].changes["title"] == {"old": None, "new": "Bulk", "redacted": False}
            assert events(note)[0].changes["owning_group_id"] == {"old": None, "new": None, "redacted": False}
        with pytest.raises(UnsafeBulkMutation):
            Note.objects.bulk_update(notes, ["title"])
        with pytest.raises(UnsafeBulkMutation):
            Note.objects.bulk_create([], ignore_conflicts=True)
        with pytest.raises(UnsafeBulkMutation):
            Note.objects.bulk_create([], update_conflicts=True, update_fields=["title"], unique_fields=["sysid"])
        Note.objects.filter(pk__in=[n.pk for n in notes]).update(title="Bulk update", body="changed secret")
        for note in notes:
            note.refresh_from_db()
            assert note.revision == 2
            assert len(events(note)) == 2
        # Raw UPDATE is audited by the database, not a save signal.
        quoted = connection.ops.quote_name(Note._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {quoted} SET title=%s,revision=revision+1 WHERE sysid=%s", ["Direct SQL", notes[0].pk.hex]
            )
        notes[0].refresh_from_db()
        assert len(events(notes[0])) == 3
        Note.objects.filter(pk__in=[n.pk for n in notes]).delete()
        assert not Note.objects.filter(pk__in=[n.pk for n in notes]).exists()
        for note in notes:
            tombstone = Note.objects.with_deleted().get(pk=note.pk)
            assert events(tombstone)[-1].operation == "soft_delete"
            tombstone.restore()
            assert events(tombstone)[-1].operation == "restore"
            restored = events(tombstone)[-1].changes["deleted_at"]
            assert restored["old"] is not None and restored["new"] is None and restored["redacted"] is False
            assert tombstone.pk == note.pk
            serialized = json.dumps([event.changes for event in events(tombstone)])
            assert "never-store-secret" not in serialized
            assert "changed secret" not in serialized
            assert "[REDACTED]" in serialized
            for event in events(tombstone):
                assert event.organization_id == records.a.pk
                assert event.request_id is not None
                assert event.source == "test:security"
                assert not {"revision", "updated_at", "created_at", "updated_by", "created_by"} & set(event.changes)


def test_restore_conflict_preserves_tombstone_and_replacement(records):
    from examples.notes.models import Note

    with actor_tenant(records.alice, records.a):
        original = Note.objects.get(pk=records.private.pk)
        original.delete()
        replacement = Note(
            organization=records.a, owner=records.alice, code=original.code, title="Replacement", body=""
        )
        replacement.save()
        with pytest.raises((ValidationError, IntegrityError)):
            original.restore()
        original.refresh_from_db()
        assert original.deleted_at is not None
        assert Note.objects.get(pk=replacement.pk).title == "Replacement"


def test_transaction_savepoint_rollbacks_remove_records_and_events(records):
    from examples.notes.models import Note

    with actor_tenant(records.alice, records.a):
        original = Note.objects.get(pk=records.private.pk)
        initial = len(events(original))
        with pytest.raises(RuntimeError):
            with transaction.atomic():
                original.title = "Rolled back"
                original.save(update_fields=["title"])
                raise RuntimeError("rollback fixture")
        assert Note.objects.get(pk=original.pk).title == "Private"
        assert len(events(original)) == initial


def test_actual_guards_reject_cross_tenant_fk_identity_and_audit_mutation(records):
    from examples.notes.models import Note
    from skeleton.audit.models import AuditEvent

    with actor_tenant(records.alice, records.a):
        table = connection.ops.quote_name(Note._meta.db_table)
        attempts = [
            (
                f"UPDATE {table} SET owning_group_id=%s,visibility=%s,revision=revision+1 WHERE sysid=%s",
                [records.foreign_group.pk.hex, "group", records.private.pk.hex],
            ),
            (
                f"UPDATE {table} SET organization_id=%s,revision=revision+1 WHERE sysid=%s",
                [records.b.pk.hex, records.private.pk.hex],
            ),
            (f"UPDATE {table} SET title=%s WHERE sysid=%s", ["No revision", records.private.pk.hex]),
            (f"DELETE FROM {table} WHERE sysid=%s", [records.private.pk.hex]),
        ]
        # Guards, tenant FKs, RLS WITH CHECK and missing runtime privileges (DELETE, audit
        # UPDATE/DELETE) all refuse inside the database; each is some DatabaseError.
        for statement, params in attempts:
            with pytest.raises(DatabaseError), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(statement, params)
        audit = connection.ops.quote_name(AuditEvent._meta.db_table)
        for statement in (
            f"UPDATE {audit} SET source=%s WHERE record_sysid=%s",
            f"DELETE FROM {audit} WHERE record_sysid=%s",
        ):
            params = ["tamper", records.private.pk.hex] if statement.startswith("UPDATE") else [records.private.pk.hex]
            with pytest.raises(DatabaseError), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(statement, params)
        assert Note.objects.get(pk=records.private.pk).revision == 1


def test_external_connection_without_context_fails_closed(records):
    import psycopg

    from examples.notes.models import Note
    from tools.integration_database import disposable_database_pair

    # A genuine second driver connection of the runtime role has no tenant GUC: RLS hides every row.
    with psycopg.connect(disposable_database_pair().runtime, options="-c search_path=skeleton,public") as external:
        changed = external.execute(
            f'UPDATE "{Note._meta.db_table}" SET title=%s,revision=revision+1 WHERE sysid=%s',
            ("external", records.private.pk),
        ).rowcount
    assert changed == 0
    with actor_tenant(records.alice, records.a):
        assert Note.objects.get(pk=records.private.pk).title == "Private"


def test_password_changes_are_detected_before_redaction(records):
    from skeleton.accounts.services import bootstrap_identity, set_local_password
    from skeleton.tenancy.context import request_context

    platform, _ = bootstrap_identity(
        email="password-platform@example.test",
        organization="password-platform",
        name="Platform",
        password="Platform-fixture-password-2026!",
    )

    def global_events():
        # Global audit is readable only with the platform GUC on (RLS of the runtime role).
        with request_context(actor=platform, request_id=uuid4(), source="test:password-audit"):
            return events(records.alice)

    before = len(global_events())
    with request_context(actor=records.alice, request_id=uuid4(), source="test:password"):
        set_local_password(records.alice, "Another-fixture-password-2026!")
    history = global_events()
    assert len(history) == before + 1
    change = history[-1].changes["password"]
    assert change["old"] == change["new"] == "[REDACTED]"
    assert change["redacted"] is True
    assert "pbkdf2" not in json.dumps(history[-1].changes)


@pytest.mark.parametrize("invalid_sysid", ["not-a-guid", "g" * 32])
def test_direct_sql_cannot_insert_non_guid_identity(records, invalid_sysid):
    from django.utils import timezone

    from examples.notes.models import Note

    with actor_tenant(records.alice, records.a):
        # PostgreSQL's uuid type refuses the value itself (DataError), before any guard runs.
        with pytest.raises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO example_notes_note "
                    "(sysid,created_at,updated_at,revision,code,title,body,organization_id,owner_id,visibility) "
                    "VALUES (%s,%s,%s,1,%s,%s,%s,%s,%s,%s)",
                    [
                        invalid_sysid,
                        timezone.now(),
                        timezone.now(),
                        "invalid-guid",
                        "Invalid",
                        "",
                        records.a.pk.hex,
                        records.alice.pk.hex,
                        "private",
                    ],
                )
        assert not Note.objects.filter(code="invalid-guid").exists()


def test_cas_and_soft_delete_hold_for_an_organization_record(records):
    """The model-level contract also holds for an OrganizationRecord without owner (#263)."""
    from skeleton.access.models import Role

    with actor_tenant(records.admin, records.a):
        role = Role(organization=records.a, code="contract-probe", name="Probe")
        role.save()
        first, second = Role.objects.get(pk=role.pk), Role.objects.get(pk=role.pk)
        first.name = "Winner"
        first.save(update_fields=["name"])
        second.name = "Loser"
        with pytest.raises(ConcurrentUpdate):
            second.save(update_fields=["name"])
        assert Role.objects.get(pk=role.pk).name == "Winner"
        stored = Role.objects.get(pk=role.pk)
        stored.delete()
        assert not Role.objects.filter(pk=role.pk).exists()
        tombstone = Role.objects.with_deleted().get(pk=role.pk)
        tombstone.restore()
        assert Role.objects.get(pk=role.pk).pk == role.pk
        audit = events(role)
    assert [event.operation for event in audit] == ["create", "update", "soft_delete", "restore"]
    assert {event.organization_id for event in audit} == {records.a.pk}


def test_cas_holds_for_a_global_system_record(records):
    """CAS of a global SystemRecord, read and audited only in the platform context (#263)."""
    from skeleton.accounts.services import bootstrap_identity
    from skeleton.system import availability
    from skeleton.system.models import AppAvailability
    from skeleton.tenancy.context import request_context

    platform, _ = bootstrap_identity(
        email="contract-platform@example.test",
        organization="contract-platform",
        name="Contract",
        password="Kq9-unrelated-Phrase-2026!",
    )
    with request_context(actor=platform, request_id=uuid4(), source="test:contract-global"):
        current = AppAvailability.objects.filter(app_label="example_notes").first()
        revision = current.revision if current else 0
        availability.set_app_availability("example_notes", user=platform, mode="selected", revision=revision)
        first = AppAvailability.objects.get(app_label="example_notes")
        second = AppAvailability.objects.get(pk=first.pk)
        first.mode = "all"
        first.save(update_fields=["mode"])
        second.mode = "selected"
        with pytest.raises(ConcurrentUpdate):
            second.save(update_fields=["mode"])
        assert AppAvailability.objects.get(pk=first.pk).mode == "all"
        audit = events(first)
    assert [event.operation for event in audit][-1] == "update"
    assert {event.organization_id for event in audit} == {None}


def test_failed_validation_does_not_poison_the_next_save(records):
    """A ValidationError leaves the metadata as it was; fixing the field saves (#273)."""
    from examples.notes.models import Note

    with actor_tenant(records.alice, records.a):
        note = Note.objects.get(pk=records.private.pk)
        revision, updated_at = note.revision, note.updated_at
        note.title = "x" * 201
        with pytest.raises(ValidationError):
            note.save()
        assert (note.revision, note.updated_at) == (revision, updated_at)
        note.title = "Fixed"
        note.save()
        assert Note.objects.get(pk=note.pk).revision == revision + 1


def test_failed_delete_keeps_the_instance_alive(records):
    from examples.notes.models import Note

    with actor_tenant(records.alice, records.a):
        first, second = Note.objects.get(pk=records.private.pk), Note.objects.get(pk=records.private.pk)
        first.title = "winner"
        first.save(update_fields=["title"])
        with pytest.raises(ConcurrentUpdate):
            second.delete()
        assert second.deleted_at is None and second.deleted_by is None
        assert Note.objects.filter(pk=second.pk).exists()


def test_failed_restore_keeps_the_tombstone_in_memory(records):
    from examples.notes.models import Note

    with actor_tenant(records.alice, records.a):
        original = Note.objects.get(pk=records.private.pk)
        original.delete()
        replacement = Note(organization=records.a, owner=records.alice, code=original.code, title="New", body="")
        replacement.save()
        tombstone = Note.objects.with_deleted().get(pk=original.pk)
        deleted_at = tombstone.deleted_at
        with pytest.raises((ValidationError, IntegrityError)):
            with transaction.atomic():
                tombstone.restore()
        assert tombstone.deleted_at == deleted_at and tombstone.deleted_by is not None
