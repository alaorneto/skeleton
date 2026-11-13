"""Operational tables: UUID and tenant isolation without per-row audit (#36).

The concrete operational models arrive with jobs, events and notifications; here a
throwaway model exercises the base, the manager, the frozen DDL and the checks.
PostgreSQL RLS of real operational tables is proved by their own suites.
"""

import uuid

import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, connection, models, transaction
from django.test.utils import isolate_apps

from skeleton.accounts.services import bootstrap_identity
from skeleton.common.exceptions import ImmutableRecord, MissingTenantContext, UnsafeBulkMutation
from skeleton.common.migration_operations import InstallRecordInfrastructure, infrastructure_names
from skeleton.common.models import OperationalRecord
from skeleton.tenancy.context import system_context, tenant_context
from skeleton.tenancy.models import Organization

pytestmark = pytest.mark.usefixtures("skeleton_db")
PASSWORD = "Operational-records-Password!29"
SPEC = {
    "scope": "organization",
    "lifecycle": "operational",
    "sensitive_fields": [],
    "columns": [
        {"name": "sysid", "type": "UUIDField", "sensitive": False},
        {"name": "created_at", "type": "DateTimeField", "sensitive": False},
        {"name": "updated_at", "type": "DateTimeField", "sensitive": False},
        {"name": "organization_id", "type": "UUIDField", "sensitive": False},
        {"name": "state", "type": "CharField", "sensitive": False},
    ],
}


@pytest.fixture
def pipeline(owner_schema_editor):
    with isolate_apps("skeleton.system.apps.SystemConfig"):

        class PipelineRow(OperationalRecord):
            organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="+")
            state = models.CharField(max_length=16, default="queued")

            class Meta(OperationalRecord.Meta):
                app_label = "sk_system"
                db_table = "sk_test_pipelinerow"

        with owner_schema_editor() as editor:
            editor.create_model(PipelineRow)
            InstallRecordInfrastructure("sk_system", "pipelinerow", SPEC)._postgres(editor, PipelineRow)
        yield PipelineRow  # The next test restores the schema template without this table.


@pytest.fixture
def tenants():
    alice, alpha = bootstrap_identity(
        email="ops-alice@example.test", organization="ops-alpha", name="Alpha", password=PASSWORD
    )
    bob, beta = bootstrap_identity(
        email="ops-bob@example.test", organization="ops-beta", name="Beta", password=PASSWORD
    )
    return alice, alpha, bob, beta


def audit_events(organization, actor, table):
    """Events of one table, counted where the organization's events are visible (#261)."""
    from skeleton.audit.models import AuditEvent

    with tenant_context(organization_id=organization.pk, actor=actor, source="test:operational-audit"):
        return AuditEvent.objects.filter(table_name=table).count()


def test_tenantless_system_context_reads_no_tenant_audit(tenants):
    from skeleton.access.models import AccessGroup
    from skeleton.audit.models import AuditEvent

    alice, alpha, _, _ = tenants
    with tenant_context(organization_id=alpha.pk, actor=alice, source="test:ops-audit-control"):
        AccessGroup(organization=alpha, code="audit-control", name="Audit control").save()
        visible = AuditEvent.objects.filter(table_name=AccessGroup._meta.db_table).count()
    assert visible >= 1  # The audited write produced an event the tenant context can read.
    with system_context(reason="test:operational-audit"):
        assert AuditEvent.all_objects.count() == 0  # No tenant and no platform GUC: RLS hides every event.


def test_update_and_delete_write_no_audit_and_manager_is_tenant_bound(pipeline, tenants):
    alice, alpha, bob, beta = tenants
    with tenant_context(organization_id=alpha.pk, actor=alice, source="test:ops-a"):
        row = pipeline(organization=alpha)
        row.save()
        row.state = "running"
        row.save(update_fields={"state"})
        assert pipeline.objects.filter(state="running").update(state="done") == 1
        with pytest.raises(UnsafeBulkMutation):
            pipeline.objects.bulk_update([row], ["state"])
        with pytest.raises(UnsafeBulkMutation):
            pipeline.objects.update(organization_id=beta.pk)
        with pytest.raises(ValidationError):
            pipeline(organization=beta).save()
    with tenant_context(organization_id=beta.pk, actor=bob, source="test:ops-b"):
        assert not pipeline.objects.exists()
    with tenant_context(organization_id=alpha.pk, actor=alice, source="test:ops-a-delete"):
        assert pipeline.objects.filter(pk=row.pk).delete()[0] == 1
    assert audit_events(alpha, alice, pipeline._meta.db_table) == 0
    from skeleton.access.models import AccessGroup

    with tenant_context(organization_id=alpha.pk, actor=alice, source="test:ops-control"):
        AccessGroup(organization=alpha, code="ops-control", name="Ops control").save()
    assert audit_events(alpha, alice, AccessGroup._meta.db_table) == 1  # The same helper sees audited writes.
    with pytest.raises(MissingTenantContext):
        list(pipeline.objects.all())


def test_guard_refuses_identity_changes(pipeline, tenants):
    alice, alpha, _, beta = tenants
    with tenant_context(organization_id=alpha.pk, actor=alice, source="test:ops-guard"):
        row = pipeline(organization=alpha)
        row.save()
        row.organization_id = beta.pk
        with pytest.raises(ImmutableRecord):
            row.save()
        table = pipeline._meta.db_table
        # Guard (23514), RLS WITH CHECK and the uuid type refuse inside PostgreSQL; one
        # savepoint per statement keeps the outer transaction usable.
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(f'UPDATE "{table}" SET organization_id=%s WHERE sysid=%s', [beta.pk.hex, row.pk.hex])
        with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(f'UPDATE "{table}" SET sysid=%s WHERE sysid=%s', [uuid.uuid4().hex, row.pk.hex])
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                f'INSERT INTO "{table}" (sysid,created_at,updated_at,organization_id,state) VALUES (%s,%s,%s,%s,%s)',
                ["not-a-uuid", row.created_at, row.created_at, alpha.pk.hex, "queued"],
            )


def test_frozen_postgres_ddl_is_operational():
    class Capture:
        def __init__(self):
            self.statements = []

        def quote_name(self, name):
            return '"' + name + '"'

        def execute(self, sql, params=None):
            self.statements.append(sql)

    with isolate_apps("skeleton.system.apps.SystemConfig"):

        class PipelineDDL(OperationalRecord):
            organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="+")
            state = models.CharField(max_length=16)

            class Meta(OperationalRecord.Meta):
                app_label = "sk_system"
                db_table = "sk_test_pipelineddl"

        operation = InstallRecordInfrastructure("sk_system", "pipelineddl", SPEC)
        capture = Capture()
        operation._postgres(capture, PipelineDDL)
        sql = "\n".join(capture.statements)
        assert "GRANT SELECT,INSERT,UPDATE,DELETE ON" in sql
        assert "BEFORE UPDATE ON" in sql and "BEFORE INSERT OR UPDATE OR DELETE" not in sql
        assert "_sk_audit_fn() RETURNS trigger" not in sql
        assert "CREATE POLICY skeleton_tenant" in sql
        assert operation.runtime_privileges(PipelineDDL) == {"SELECT", "INSERT", "UPDATE", "DELETE"}
    assert infrastructure_names("t", operational=True) == ("t_sk_guard",)
    assert infrastructure_names("t") == ("t_sk_guard", "t_sk_audit")


def test_operational_model_cannot_be_exposed(monkeypatch):
    from skeleton.system import registry
    from skeleton.system.checks import record_contract_checks

    with isolate_apps("skeleton.system.apps.SystemConfig") as isolated:

        class PipelineExposed(OperationalRecord):
            state = models.CharField(max_length=16)

            class Meta(OperationalRecord.Meta):
                app_label = "sk_system"

        monkeypatch.setitem(
            registry._registry,
            PipelineExposed._meta.label_lower,
            registry.TableSpec(model=PipelineExposed, description="x", scope="global", expose_api=True),
        )
        ids = [error.id for error in record_contract_checks(apps_registry=isolated)]
    assert "skeleton.E020" in ids
    assert "skeleton.E001" not in ids


def test_operational_update_does_not_reread_the_identity(pipeline, tenants):
    """The identity snapshot of a loaded or saved row replaces the extra SELECT (#341)."""
    from django.test.utils import CaptureQueriesContext

    alice, alpha, _, _ = tenants
    with tenant_context(organization_id=alpha.pk, actor=alice, source="test:ops-snapshot"):
        row = pipeline(organization=alpha)
        row.save()
        for instance in (row, pipeline.objects.get(pk=row.pk)):
            instance.state = "running"
            with CaptureQueriesContext(connection) as captured:
                instance.save(update_fields={"state"})
            assert [query["sql"].split()[0] for query in captured] == ["UPDATE"]
        fresh = pipeline.objects.get(pk=row.pk)
        fresh.organization_id = uuid.uuid4()
        with pytest.raises(ImmutableRecord):
            fresh.save()


@pytest.mark.parametrize(
    "field,declared,expected",
    [
        ("text", False, True),
        ("relation", False, False),
        ("text", True, False),
    ],
)
def test_e008_follows_audit_sensitive_names(monkeypatch, field, declared, expected):
    """A secret named as the audit redacts it (token, recovery_codes...) must be declared; a relation needs not (#310)."""
    from skeleton.system import registry
    from skeleton.system.checks import record_contract_checks

    with isolate_apps("skeleton.system.apps.SystemConfig") as isolated:
        if field == "text":

            class Secretive(OperationalRecord):
                token = models.CharField(max_length=64)

                class Meta(OperationalRecord.Meta):
                    app_label = "sk_system"

        else:

            class Secretive(OperationalRecord):
                token = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="+")

                class Meta(OperationalRecord.Meta):
                    app_label = "sk_system"

        monkeypatch.setitem(
            registry._registry,
            Secretive._meta.label_lower,
            registry.TableSpec(
                model=Secretive,
                description="x",
                scope="global",
                sensitive_fields=("token",) if declared else (),
            ),
        )
        errors = [
            error
            for error in record_contract_checks(apps_registry=isolated)
            if error.id == "skeleton.E008" and "Secretive" in error.msg
        ]
    assert bool(errors) is expected, errors
