"""Children frozen by the seal of their parent (#528): model, database guard and the race with two connections."""

import hashlib
import uuid
from types import SimpleNamespace

import psycopg
import pytest
from django.db import IntegrityError, OperationalError, connection, models, transaction
from django.test.utils import isolate_apps
from django.utils import timezone

from skeleton.accounts.services import bootstrap_identity
from skeleton.common.exceptions import RecordSealed
from skeleton.common.migration_operations import InstallRecordInfrastructure, build_infrastructure_specification
from skeleton.common.models import OrganizationRecord, SealableMixin
from skeleton.tenancy.context import tenant_context
from skeleton.tenancy.models import Organization

pytestmark = pytest.mark.usefixtures("skeleton_db")
PASSWORD = "Sealed-children-Password!32"
TABLE_SPEC = SimpleNamespace(scope="organization", sensitive_fields=(), personal_fields=())
DIGEST = hashlib.sha256(b"parent").hexdigest()


@pytest.fixture
def probes(owner_schema_editor):
    with isolate_apps("skeleton.system.apps.SystemConfig"):

        class ParentProbe(SealableMixin, OrganizationRecord):
            organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="+")
            body = models.TextField(blank=True, default="")
            seal_fields = ("body",)

            class Meta(OrganizationRecord.Meta):
                app_label = "sk_system"
                db_table = "sk_test_parentprobe"

        class ChildProbe(OrganizationRecord):
            organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="+")
            document = models.ForeignKey(ParentProbe, on_delete=models.PROTECT, related_name="+")
            text = models.TextField(blank=True, default="")
            seal_parent = "document"
            immutable_fields = ("document_id",)

            class Meta(OrganizationRecord.Meta):
                app_label = "sk_system"
                db_table = "sk_test_childprobe"

        with owner_schema_editor() as editor:
            for model in (ParentProbe, ChildProbe):
                editor.create_model(model)
                specification = build_infrastructure_specification(model, TABLE_SPEC)
                InstallRecordInfrastructure("sk_system", model._meta.model_name, specification)._postgres(editor, model)
        yield SimpleNamespace(parent=ParentProbe, child=ChildProbe)


@pytest.fixture
def tenant():
    alice, alpha = bootstrap_identity(
        email="children-alice@example.test", organization="children-alpha", name="Alpha", password=PASSWORD
    )
    return alice, alpha


def in_tenant(organization, actor):
    return tenant_context(organization_id=organization.pk, actor=actor, source="test:sealed-children")


def seal(parent, actor):
    parent.apply_seal(actor_id=actor.pk, digest=DIGEST, at=timezone.now())
    parent.save(update_fields={"sealed_at", "sealed_by", "seal_digest"})


def sql_error(statement, params):
    with pytest.raises(IntegrityError) as error, transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(statement, params)
    return str(error.value)


def test_children_follow_the_parent_seal(probes, tenant):
    alice, alpha = tenant
    with in_tenant(alpha, alice):
        parent = probes.parent.objects.create(organization=alpha, body="x")
        other = probes.parent.objects.create(organization=alpha, body="y")
        child = probes.child.objects.create(organization=alpha, document=parent, text="line 1")
        child.text = "line 1, edited"
        child.save()  # Open parent: as today.
        seal(parent, alice)
        child = probes.child.objects.get(pk=child.pk)
        child.text = "tampered"
        with pytest.raises(RecordSealed):
            child.save()
        with pytest.raises(RecordSealed):
            probes.child.objects.create(organization=alpha, document=parent, text="late line")
        with pytest.raises(RecordSealed):
            probes.child.objects.get(pk=child.pk).delete()
        table = "skeleton.sk_test_childprobe"
        assert "sealed parent" in sql_error(
            f"UPDATE {table} SET text='x', revision=revision+1 WHERE sysid=%s", [child.pk]
        )
        assert "sealed parent" in sql_error(
            f"UPDATE {table} SET deleted_at=now(), revision=revision+1 WHERE sysid=%s", [child.pk]
        )
        assert "sealed parent" in sql_error(
            f"INSERT INTO {table} (sysid, created_at, updated_at, revision, organization_id, document_id, text) "
            "VALUES (%s, now(), now(), 1, %s, %s, 'raw')",
            [uuid.uuid4(), alpha.pk, parent.pk],
        )
        loose = probes.child.objects.create(organization=alpha, document=other, text="other")
        # Moving a child to a sealed parent fails: the key is immutable (E040 requires it).
        assert "immutable record identity" in sql_error(
            f"UPDATE {table} SET document_id=%s, revision=revision+1 WHERE sysid=%s", [parent.pk, loose.pk]
        )
        loose.text = "still free"
        loose.save()


def test_a_deleted_child_of_a_sealed_parent_cannot_be_restored(probes, tenant):
    alice, alpha = tenant
    with in_tenant(alpha, alice):
        parent = probes.parent.objects.create(organization=alpha, body="x")
        child = probes.child.objects.create(organization=alpha, document=parent, text="dropped")
        child.delete()
        seal(parent, alice)
        deleted = probes.child.objects.with_deleted().get(pk=child.pk)
        with pytest.raises(RecordSealed):
            deleted.restore()


def runtime_connection(organization, actor):
    """A second runtime-role session with the tenant GUCs, for the race (#528)."""
    settings = connection.settings_dict
    second = psycopg.connect(
        host=settings["HOST"],
        port=settings["PORT"],
        dbname=settings["NAME"],
        user=settings["USER"],
        password=settings["PASSWORD"],
        options="-c search_path=skeleton,public",
    )
    second.execute("SELECT set_config('skeleton.tenant_id', %s, false)", [str(organization.pk)])
    second.execute("SELECT set_config('skeleton.user_id', %s, false)", [str(actor.pk)])
    second.execute("SET lock_timeout = '300ms'")
    second.commit()
    return second


def insert_child(second, organization, parent):
    second.execute(
        "INSERT INTO skeleton.sk_test_childprobe (sysid, created_at, updated_at, revision, organization_id, document_id, text)"
        " VALUES (%s, now(), now(), 1, %s, %s, 'concurrent')",
        [uuid.uuid4(), organization.pk, parent.pk],
    )


def test_a_child_written_while_the_parent_is_being_sealed_waits_and_is_refused(probes, tenant):
    alice, alpha = tenant
    with in_tenant(alpha, alice):
        parent = probes.parent.objects.create(organization=alpha, body="x")
    second = runtime_connection(alpha, alice)
    try:
        with in_tenant(alpha, alice), transaction.atomic():
            locked = probes.parent.objects.select_for_update().get(pk=parent.pk)
            seal(locked, alice)
            # The guard reads the parent FOR SHARE: it waits for the sealing transaction.
            with pytest.raises(psycopg.errors.LockNotAvailable):
                insert_child(second, alpha, parent)
            second.rollback()
        with pytest.raises(psycopg.errors.CheckViolation, match="sealed parent"):
            insert_child(second, alpha, parent)
        second.rollback()
    finally:
        second.close()


def test_a_child_committed_first_holds_the_seal_until_it_commits(probes, tenant):
    alice, alpha = tenant
    with in_tenant(alpha, alice):
        parent = probes.parent.objects.create(organization=alpha, body="x")
    second = runtime_connection(alpha, alice)
    try:
        insert_child(second, alpha, parent)  # Holds FOR SHARE on the parent until it commits.
        with in_tenant(alpha, alice):
            with pytest.raises(OperationalError), transaction.atomic():
                connection.cursor().execute("SET LOCAL lock_timeout = '300ms'")
                probes.parent.objects.select_for_update().get(pk=parent.pk)
        second.commit()
        with in_tenant(alpha, alice), transaction.atomic():
            locked = probes.parent.objects.select_for_update().get(pk=parent.pk)
            assert probes.child.objects.filter(document=locked).count() == 1  # Read by whoever seals.
            seal(locked, alice)
    finally:
        second.close()


def test_a_child_of_another_organization_never_reaches_the_parent(probes, tenant):
    from django.core.exceptions import ValidationError

    alice, alpha = tenant
    bob, beta = bootstrap_identity(
        email="children-bob@example.test", organization="children-beta", name="Beta", password=PASSWORD
    )
    with in_tenant(alpha, alice):
        parent = probes.parent.objects.create(organization=alpha, body="x")
    with in_tenant(beta, bob):
        assert not probes.parent.all_objects.filter(pk=parent.pk).exists()  # RLS
        with pytest.raises((ValidationError, IntegrityError)), transaction.atomic():
            probes.child.objects.create(organization=beta, document_id=parent.pk, text="foreign")
