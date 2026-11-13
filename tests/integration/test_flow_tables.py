"""Record contract of the six flow tables (#202), on PostgreSQL with the runtime role.

The rows are written directly, as the engine will write them, to prove what the database and
the record bases guarantee before any service exists: catalog, tenant isolation, same-tenant
references, audit with redaction, CAS, immutability and no physical delete.
"""

import uuid

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import DatabaseError, IntegrityError, connection, transaction

from skeleton.audit.models import AuditEvent
from skeleton.common.exceptions import ConcurrentUpdate, ImmutableRecord, MissingTenantContext
from skeleton.flows.models import (
    FlowActivity,
    FlowDefinition,
    FlowInstance,
    FlowStepRun,
    FlowSubmission,
    FlowTask,
)
from skeleton.tenancy.context import system_context
from tools.security_fixtures import actor_tenant, build_security_fixture

pytestmark = pytest.mark.needs_example_app

MODELS = (FlowDefinition, FlowInstance, FlowStepRun, FlowTask, FlowSubmission, FlowActivity)
SECRET = "segredo-do-formulario"


@pytest.fixture
def tenants(skeleton_db):
    return build_security_fixture()


def build_rows(organization, subject, owner):
    """One row of each table, linked like a running instance with one open task."""
    definition = FlowDefinition(
        organization=organization,
        key="example_notes.review",
        version=1,
        app_label="example_notes",
        label="Revisão",
        digest="0" * 64,
        specification={"key": "example_notes.review"},
    )
    definition.save()
    instance = FlowInstance(
        organization=organization,
        definition=definition,
        flow_key=definition.key,
        flow_version=1,
        subject_table="example_notes.note",
        subject_sysid=subject.pk,
        starter=owner,
        data={"audience": "public"},
        sensitive_data={"remark": SECRET},
    )
    instance.save()
    run = FlowStepRun(organization=organization, instance=instance, step_key="prepare", kind="task", sequence=1)
    run.save()
    task = FlowTask(
        organization=organization,
        instance=instance,
        step_run=run,
        step_key="prepare",
        kind="task",
        assignee=owner,
        comment=SECRET,
    )
    task.save()
    submission = FlowSubmission(
        organization=organization,
        instance=instance,
        step_run=run,
        task=task,
        step_key="prepare",
        origin="task",
        values={"audience": "public"},
        sensitive_values={"remark": SECRET},
    )
    submission.save()
    activity = FlowActivity(
        organization=organization, instance=instance, sequence=1, kind="task_completed", task=task, comment=SECRET
    )
    activity.save()
    return definition, instance, run, task, submission, activity


@pytest.fixture
def rows(tenants):
    with system_context(reason="test:flow-tables", organization_id=tenants.a.pk):
        in_a = build_rows(tenants.a, tenants.private, tenants.alice)
    with system_context(reason="test:flow-tables", organization_id=tenants.b.pk):
        in_b = build_rows(tenants.b, tenants.foreign, tenants.bob)
    return in_a, in_b


def test_tables_are_cataloged_and_the_schema_check_passes(tenants):
    from skeleton.system.models import TableDefinition

    for model in MODELS:
        entry = TableDefinition.objects.get(model_label=model._meta.label_lower)
        assert (entry.scope, entry.base_class, entry.uuid_managed, entry.soft_delete, entry.api_exposed) == (
            "organization",
            "OrganizationRecord",
            True,
            True,
            False,
        )
        assert entry.available and entry.origin == "core"
    call_command("check_skeleton_schema", database="default", verbosity=0)


def test_without_a_tenant_reads_and_writes_fail_closed(rows):
    for model in MODELS:
        with pytest.raises(MissingTenantContext):
            model.objects.count()
    with pytest.raises(MissingTenantContext), system_context(reason="test:flow-global"):
        FlowDefinition(key="k.k", version=1, app_label="k", label="k", digest="1" * 64, specification={}).save()
    with connection.cursor() as cursor:  # RLS without the tenant GUC: no row is visible.
        for model in MODELS:
            cursor.execute(f'SELECT count(*) FROM "{model._meta.db_table}"')
            assert cursor.fetchone()[0] == 0


def test_organizations_do_not_see_or_change_each_other(tenants, rows):
    in_a, in_b = rows
    with actor_tenant(tenants.alice, tenants.a):
        for model, own, foreign in zip(MODELS, in_a, in_b, strict=True):
            assert list(model.objects.values_list("pk", flat=True)) == [own.pk]
            assert not model.objects.filter(pk=foreign.pk).exists()
            assert not model.objects.with_deleted().filter(pk=foreign.pk).exists()
            # The base manager is unfiltered in Python; RLS still hides the other tenant.
            assert not model.all_objects.filter(pk=foreign.pk).exists()
        with connection.cursor() as cursor:
            cursor.execute(
                f'UPDATE "{FlowTask._meta.db_table}" SET comment=%s, revision=revision+1 WHERE sysid=%s',
                ["taken", in_b[3].pk.hex],
            )
            assert cursor.rowcount == 0
    with actor_tenant(tenants.bob, tenants.b):
        assert FlowTask.objects.get(pk=in_b[3].pk).comment == SECRET


def test_references_across_organizations_are_refused(tenants, rows):
    in_a, in_b = rows
    with system_context(reason="test:flow-cross", organization_id=tenants.a.pk):
        with pytest.raises(ValidationError), transaction.atomic():
            FlowStepRun(organization=tenants.a, instance=in_b[1], step_key="x", kind="task", sequence=9).save()
        # Even past the model, the composite foreign key refuses the pair (organization, instance).
        with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                f'INSERT INTO "{FlowActivity._meta.db_table}" '
                "(sysid, created_at, updated_at, revision, organization_id, instance_id, sequence, kind, step_key,"
                " detail, comment) VALUES (%s, now(), now(), 1, %s, %s, 99, 'x', '', '{}'::jsonb, '')",
                [uuid.uuid4().hex, tenants.a.pk.hex, in_b[1].pk.hex],
            )
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(  # RLS WITH CHECK: a row of B cannot be written from A.
                f'INSERT INTO "{FlowActivity._meta.db_table}" '
                "(sysid, created_at, updated_at, revision, organization_id, instance_id, sequence, kind, step_key,"
                " detail, comment) VALUES (%s, now(), now(), 1, %s, %s, 99, 'x', '', '{}'::jsonb, '')",
                [uuid.uuid4().hex, tenants.b.pk.hex, in_b[1].pk.hex],
            )


def events_of(record):
    return list(AuditEvent.objects.filter(record_sysid=record.pk).order_by("created_at", "record_revision"))


def test_every_change_is_audited_once_with_secrets_redacted(tenants, rows):
    (definition, instance, run, task, submission, activity), _ = rows
    with actor_tenant(tenants.admin, tenants.a):
        for record in (definition, instance, run, task, submission, activity):
            [created] = events_of(record)
            assert (created.operation, created.record_revision) == ("create", 1)
            assert created.organization_id == tenants.a.pk
            assert created.source == "system:test:flow-tables"
            assert SECRET not in str(created.changes)
        assert events_of(instance)[0].changes["sensitive_data"] == {
            "old": None,
            "new": "[REDACTED]",
            "redacted": True,
        }
        assert events_of(instance)[0].changes["data"]["new"] == {"audience": "public"}
        assert events_of(submission)[0].changes["sensitive_values"]["new"] == "[REDACTED]"
        assert events_of(task)[0].changes["comment"]["new"] == "[REDACTED]"
        assert events_of(activity)[0].changes["comment"]["new"] == "[REDACTED]"

        task = FlowTask.objects.get(pk=task.pk)
        task.status, task.comment = "completed", "outro " + SECRET
        task.save(update_fields={"status", "comment"})
        update = events_of(task)[-1]
        assert (update.operation, update.record_revision, update.actor_sysid) == ("update", 2, tenants.admin.pk)
        assert set(update.changes) == {"status", "comment"}
        assert update.changes["status"] == {"old": "open", "new": "completed", "redacted": False}
        assert update.changes["comment"] == {"old": "[REDACTED]", "new": "[REDACTED]", "redacted": True}

        before = AuditEvent.objects.count()
        task.save(update_fields=set())  # Nothing to write: no revision, no event.
        assert AuditEvent.objects.count() == before and FlowTask.objects.get(pk=task.pk).revision == 2

        activity = FlowActivity.objects.get(pk=activity.pk)
        activity.delete()
        assert not FlowActivity.objects.filter(pk=activity.pk).exists()
        FlowActivity.objects.with_deleted().get(pk=activity.pk).restore()
        assert [event.operation for event in events_of(activity)] == ["create", "soft_delete", "restore"]
        assert FlowActivity.objects.get(pk=activity.pk).revision == 3


def test_rollback_leaves_no_row_and_no_audit_event(tenants, rows):
    (definition, instance, *_), _ = rows
    with actor_tenant(tenants.admin, tenants.a):
        before = AuditEvent.objects.count()
        with pytest.raises(RuntimeError), transaction.atomic():
            FlowStepRun(organization=tenants.a, instance=instance, step_key="x", kind="timer", sequence=2).save()
            raise RuntimeError("caller failed")
        assert not FlowStepRun.objects.filter(sequence=2).exists()
        assert AuditEvent.objects.count() == before


def test_two_writes_at_the_same_revision_have_one_winner(tenants, rows):
    (_, instance, *_), _ = rows
    with actor_tenant(tenants.admin, tenants.a):
        first, second = FlowInstance.objects.get(pk=instance.pk), FlowInstance.objects.get(pk=instance.pk)
        first.status = "suspended"
        first.save(update_fields={"status"})
        second.status = "cancelled"
        with pytest.raises(ConcurrentUpdate):
            second.save(update_fields={"status"})
        stored = FlowInstance.objects.get(pk=instance.pk)
        assert (stored.status, stored.revision) == ("suspended", 2)


def test_immutable_fields_are_guarded_in_python_and_in_the_database(tenants, rows):
    (definition, instance, run, task, submission, activity), _ = rows
    changes = (
        (definition, "specification", {"key": "tampered"}),
        (definition, "digest", "f" * 64),
        (instance, "subject_sysid", uuid.uuid4()),
        (instance, "flow_key", "other.flow"),
        (run, "step_key", "other"),
        (task, "slot", 7),
        (submission, "values", {"audience": "internal"}),
        (activity, "kind", "rewritten"),
    )
    with actor_tenant(tenants.admin, tenants.a):
        for record, name, value in changes:
            fresh = type(record).objects.get(pk=record.pk)
            setattr(fresh, name, value)
            with pytest.raises(ImmutableRecord):
                fresh.save()
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                f'UPDATE "{FlowDefinition._meta.db_table}" SET digest=%s, revision=revision+1 WHERE sysid=%s',
                ["f" * 64, definition.pk.hex],
            )
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(  # A revision that does not increment by one is refused by the guard.
                f'UPDATE "{FlowTask._meta.db_table}" SET status=%s WHERE sysid=%s', ["cancelled", task.pk.hex]
            )
        assert FlowDefinition.objects.get(pk=definition.pk).digest == "0" * 64


def test_the_runtime_role_cannot_delete_physically(tenants, rows):
    (*_, activity), _ = rows
    with actor_tenant(tenants.admin, tenants.a):
        for model in MODELS:
            with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(f'DELETE FROM "{model._meta.db_table}"')
        assert FlowActivity.objects.filter(pk=activity.pk).exists()


def test_integrity_constraints_of_the_tables(tenants, rows):
    (definition, instance, run, task, *_), _ = rows
    with system_context(reason="test:flow-constraints", organization_id=tenants.a.pk):
        with pytest.raises(ValidationError), transaction.atomic():
            FlowDefinition(
                organization=tenants.a,
                key=definition.key,
                version=1,
                app_label="example_notes",
                label="Outra",
                digest="2" * 64,
                specification={},
            ).save()
        with pytest.raises(ValidationError), transaction.atomic():  # One active instance per flow and subject.
            FlowInstance(
                organization=tenants.a,
                definition=definition,
                flow_key=definition.key,
                flow_version=1,
                subject_table="example_notes.note",
                subject_sysid=tenants.private.pk,
            ).save()
        with pytest.raises(ValidationError), transaction.atomic():
            FlowStepRun(organization=tenants.a, instance=instance, step_key="again", kind="task", sequence=1).save()
        with pytest.raises((ValidationError, IntegrityError)), transaction.atomic():
            FlowTask(
                organization=tenants.a,
                instance=instance,
                step_run=run,
                step_key="prepare",
                kind="task",
                decision="maybe",
            ).save()
        # A finished instance frees the subject for a new one.
        fresh = FlowInstance.objects.get(pk=instance.pk)
        fresh.status = "completed"
        fresh.save(update_fields={"status"})
        FlowInstance(
            organization=tenants.a,
            definition=definition,
            flow_key=definition.key,
            flow_version=1,
            subject_table="example_notes.note",
            subject_sysid=tenants.private.pk,
        ).save()
