"""Sealed records (#526): frozen columns after the seal, enforced by the model and by the database guard.

Throwaway probe tables are created by the owner; every assertion runs as ``skeleton_runtime``.
"""

import hashlib
import uuid
from types import SimpleNamespace

import pytest
from django.db import IntegrityError, connection, models, transaction
from django.test.utils import isolate_apps
from django.utils import timezone

from skeleton.accounts.services import bootstrap_identity
from skeleton.common.exceptions import RecordSealed
from skeleton.common.migration_operations import InstallRecordInfrastructure, build_infrastructure_specification
from skeleton.common.models import OrganizationRecord, SealableMixin
from skeleton.tenancy.context import tenant_context
from skeleton.tenancy.models import Organization

pytestmark = pytest.mark.usefixtures("skeleton_db")
PASSWORD = "Sealed-records-Password!31"
TABLE_SPEC = SimpleNamespace(scope="organization", sensitive_fields=(), personal_fields=())
DIGEST = hashlib.sha256(b"sealed").hexdigest()


def specification(model):
    return build_infrastructure_specification(model, TABLE_SPEC)


@pytest.fixture
def probes(owner_schema_editor):
    with isolate_apps("skeleton.system.apps.SystemConfig"):

        class SealProbe(SealableMixin, OrganizationRecord):
            organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="+")
            title = models.CharField(max_length=100, default="", blank=True)
            body = models.TextField(default="", blank=True)
            status = models.CharField(max_length=20, default="draft")
            seal_fields = ("body",)

            class Meta(OrganizationRecord.Meta):
                app_label = "sk_system"
                db_table = "sk_test_sealprobe"

        class DeletableSealProbe(SealableMixin, OrganizationRecord):
            organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="+")
            body = models.TextField(default="", blank=True)
            seal_fields = ("body",)
            seal_allows_delete = True

            class Meta(OrganizationRecord.Meta):
                app_label = "sk_system"
                db_table = "sk_test_deletablesealprobe"

        class ConditionSealProbe(SealableMixin, OrganizationRecord):
            organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="+")
            body = models.TextField(default="", blank=True)
            status = models.CharField(max_length=20, default="draft")
            seal_fields = ("body", "status")
            seal_condition_sql = "NEW.status = 'final'"

            class Meta(OrganizationRecord.Meta):
                app_label = "sk_system"
                db_table = "sk_test_conditionsealprobe"

        models_ = (SealProbe, DeletableSealProbe, ConditionSealProbe)
        with owner_schema_editor() as editor:
            for model in models_:
                editor.create_model(model)
                InstallRecordInfrastructure("sk_system", model._meta.model_name, specification(model))._postgres(
                    editor, model
                )
        yield SimpleNamespace(plain=SealProbe, deletable=DeletableSealProbe, condition=ConditionSealProbe)


@pytest.fixture
def tenants():
    alice, alpha = bootstrap_identity(
        email="seal-alice@example.test", organization="seal-alpha", name="Alpha", password=PASSWORD
    )
    bob, beta = bootstrap_identity(
        email="seal-bob@example.test", organization="seal-beta", name="Beta", password=PASSWORD
    )
    return alice, alpha, bob, beta


def in_tenant(organization, actor):
    return tenant_context(organization_id=organization.pk, actor=actor, source="test:sealed")


def sealed(model, organization, actor, **values):
    record = model.objects.create(organization=organization, **values)
    record.apply_seal(actor_id=actor.pk, digest=DIGEST, at=timezone.now())
    record.save(update_fields={"sealed_at", "sealed_by", "seal_digest"})
    return record


def sql(statement, params=()):
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(statement, params)
        return cursor.rowcount


def database_error(statement, params=()):
    with pytest.raises(IntegrityError) as error:
        sql(statement, params)
    return str(error.value)


def test_an_unsealed_record_stays_editable(probes, tenants):
    alice, alpha, _, _ = tenants
    with in_tenant(alpha, alice):
        record = probes.plain.objects.create(organization=alpha, body="draft")
        record.body, record.title = "second draft", "Title"
        record.save()
        assert record.revision == 2
        assert sql(
            "UPDATE skeleton.sk_test_sealprobe SET body='third', revision=revision+1 WHERE sysid=%s", [record.pk]
        )


def test_frozen_columns_refuse_every_writer_after_the_seal(probes, tenants):
    alice, alpha, _, _ = tenants
    with in_tenant(alpha, alice):
        record = sealed(probes.plain, alpha, alice, body="signed")
        for change in ({"body": "tampered"}, {"seal_digest": "0" * 64}, {"sealed_by": uuid.uuid4()}):
            fresh = probes.plain.objects.get(pk=record.pk)
            for name, value in change.items():
                setattr(fresh, name, value)
            with pytest.raises(RecordSealed):
                fresh.save()
        queryset = probes.plain.all_objects.filter(pk=record.pk)
        with pytest.raises(IntegrityError, match="sealed record"), transaction.atomic():
            models.QuerySet.update(queryset, body="tampered", revision=models.F("revision") + 1)
        table = "skeleton.sk_test_sealprobe"
        for assignment in ("body='x'", "sealed_at=now()", "sealed_by=NULL", "seal_digest=repeat('a',64)"):
            message = database_error(
                f"UPDATE {table} SET {assignment}, revision=revision+1 WHERE sysid=%s", [record.pk]
            )
            assert "sealed record" in message
        assert probes.plain.objects.get(pk=record.pk).body == "signed"


def test_free_columns_change_with_revision_and_audit_after_the_seal(probes, tenants):
    from skeleton.audit.models import AuditEvent

    alice, alpha, _, _ = tenants
    with in_tenant(alpha, alice):
        record = sealed(probes.plain, alpha, alice, body="signed")
        before = AuditEvent.objects.filter(record_sysid=record.pk).count()
        record.status, record.title = "delivered", "Delivered"
        record.save()
        assert record.revision == 3
        events = AuditEvent.objects.filter(record_sysid=record.pk).order_by("created_at")
        assert events.count() == before + 1
        assert set(events.last().changes) >= {"status", "title"}


def test_soft_delete_is_refused_unless_the_model_allows_it(probes, tenants):
    alice, alpha, _, _ = tenants
    with in_tenant(alpha, alice):
        record = sealed(probes.plain, alpha, alice, body="signed")
        with pytest.raises(RecordSealed):
            record.delete()
        assert probes.plain.objects.filter(pk=record.pk).exists()
        message = database_error(
            "UPDATE skeleton.sk_test_sealprobe SET deleted_at=now(), revision=revision+1 WHERE sysid=%s", [record.pk]
        )
        assert "sealed record" in message
        deletable = sealed(probes.deletable, alpha, alice, body="signed")
        deletable.delete()
        assert not probes.deletable.objects.filter(pk=deletable.pk).exists()
        deletable.restore()
        assert probes.deletable.objects.get(pk=deletable.pk).body == "signed"


def test_an_invalid_seal_transition_is_refused(probes, tenants):
    alice, alpha, _, _ = tenants
    table = "skeleton.sk_test_sealprobe"
    with in_tenant(alpha, alice):
        record = probes.plain.objects.create(organization=alpha, body="draft")
        for assignment, params in (
            ("sealed_at=now(), sealed_by=NULL, seal_digest=%s", [DIGEST]),
            ("sealed_at=now(), sealed_by=%s, seal_digest='abc'", [str(alice.pk)]),
            ("sealed_at=now(), sealed_by=%s, seal_digest=%s", [str(alice.pk), DIGEST.upper()]),
            ("sealed_at=now(), sealed_by=%s, seal_digest=%s, body='changed'", [str(alice.pk), DIGEST]),
            ("sealed_by=%s", [str(alice.pk)]),
        ):
            message = database_error(
                f"UPDATE {table} SET {assignment}, revision=revision+1 WHERE sysid=%s", [*params, record.pk]
            )
            assert "invalid seal" in message
        record.body = "changed while sealing"
        record.apply_seal(actor_id=alice.pk, digest=DIGEST, at=timezone.now())
        with pytest.raises(RecordSealed):
            record.save()
        with pytest.raises(RecordSealed):
            probes.plain(organization=alpha).apply_seal(actor_id=None, digest=DIGEST, at=timezone.now())


def test_a_record_inserted_already_sealed_is_accepted(probes, tenants):
    alice, alpha, _, _ = tenants
    with in_tenant(alpha, alice):
        record = probes.plain(organization=alpha, body="final")
        record.apply_seal(actor_id=alice.pk, digest=DIGEST, at=timezone.now())
        record.save()
        assert probes.plain.objects.get(pk=record.pk).sealed_at is not None
        broken = probes.plain(organization=alpha, body="x", sealed_at=timezone.now())
        with pytest.raises(RecordSealed):
            broken.save()


def test_the_seal_condition_is_checked_by_the_database(probes, tenants):
    alice, alpha, _, _ = tenants
    with in_tenant(alpha, alice):
        draft = probes.condition.objects.create(organization=alpha, body="x", status="draft")
        draft.apply_seal(actor_id=alice.pk, digest=DIGEST, at=timezone.now())
        with pytest.raises(IntegrityError, match="seal condition failed"), transaction.atomic():
            draft.save()
        final = probes.condition.objects.create(organization=alpha, body="x", status="final")
        final.apply_seal(actor_id=alice.pk, digest=DIGEST, at=timezone.now())
        final.save()
        assert probes.condition.objects.get(pk=final.pk).sealed_at is not None


@pytest.mark.parametrize("condition", ["true; DROP TABLE x", "true -- x", "true /* x */", "$$ x $$", "$guard$ true"])
def test_an_unsafe_condition_is_refused_before_any_ddl(condition):
    specification = {
        "scope": "organization",
        "sensitive_fields": [],
        "seal": {"fields": ["body"], "allow_delete": False, "condition_sql": condition},
    }
    with pytest.raises(ValueError, match="condition_sql"):
        InstallRecordInfrastructure("sk_system", "probe", specification)


def test_the_sealing_write_is_audited_and_a_rollback_leaves_no_event(probes, tenants):
    from skeleton.audit.models import AuditEvent

    alice, alpha, _, _ = tenants
    with in_tenant(alpha, alice):
        record = probes.plain.objects.create(organization=alpha, body="signed")
        with pytest.raises(RuntimeError), transaction.atomic():
            record.apply_seal(actor_id=alice.pk, digest=DIGEST, at=timezone.now())
            record.save(update_fields={"sealed_at", "sealed_by", "seal_digest"})
            raise RuntimeError("rollback")
        assert not AuditEvent.objects.filter(record_sysid=record.pk, changes__seal_digest__new=DIGEST).exists()
        fresh = probes.plain.objects.get(pk=record.pk)
        fresh.apply_seal(actor_id=alice.pk, digest=DIGEST, at=timezone.now())
        fresh.save(update_fields={"sealed_at", "sealed_by", "seal_digest"})
        event = AuditEvent.objects.get(record_sysid=record.pk, changes__seal_digest__new=DIGEST)
        assert event.changes["seal_digest"]["new"] == DIGEST and event.changes["seal_digest"]["redacted"] is False


def test_another_organization_neither_reads_nor_seals_the_record(probes, tenants):
    alice, alpha, bob, beta = tenants
    with in_tenant(alpha, alice):
        record = probes.plain.objects.create(organization=alpha, body="draft")
    with in_tenant(beta, bob):
        assert not probes.plain.all_objects.filter(pk=record.pk).exists()
        updated = sql(
            "UPDATE skeleton.sk_test_sealprobe SET sealed_at=now(), sealed_by=%s, seal_digest=%s, revision=revision+1"
            " WHERE sysid=%s",
            [str(bob.pk), DIGEST, record.pk],
        )
        assert updated == 0
    with in_tenant(alpha, alice):
        assert probes.plain.objects.get(pk=record.pk).sealed_at is None


def test_the_schema_check_reports_a_guard_reinstalled_without_the_seal(probes, owner_schema_editor, monkeypatch):
    from skeleton.system.checks import schema

    model = probes.plain
    operation = InstallRecordInfrastructure("sk_system", "sealprobe", specification(model))
    real_specs, real_frozen = schema.all_specs, schema._frozen_operations
    probe_spec = SimpleNamespace(model=model, sensitive_fields=(), personal_fields=())
    monkeypatch.setattr(schema, "all_specs", lambda: [*real_specs(), probe_spec])

    def frozen(*args, **kwargs):
        operations, privileges, lifecycle = real_frozen(*args, **kwargs)
        return {**operations, model._meta.label_lower: operation}, privileges, lifecycle

    monkeypatch.setattr(schema, "_frozen_operations", frozen)

    def guard_errors():
        return [e for e in schema.check_deployed_schema() if e.startswith("sk_test_sealprobe:") and "guard" in e]

    assert guard_errors() == []
    without_seal = {key: value for key, value in specification(model).items() if key != "seal"}
    with owner_schema_editor() as editor:
        InstallRecordInfrastructure("sk_system", "sealprobe", without_seal)._install_guard(editor, model)
    assert guard_errors() == [
        "sk_test_sealprobe: exact guard function body/owner/security/search_path/EXECUTE inventory mismatch."
    ]
