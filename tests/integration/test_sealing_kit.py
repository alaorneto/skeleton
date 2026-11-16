"""The seal contract kit (#530) on a core fixture model and on a test model of the example app."""

import hashlib
from types import SimpleNamespace

import pytest
from django.db import models
from django.test.utils import isolate_apps
from django.utils import timezone

from skeleton.accounts.services import bootstrap_identity
from skeleton.common.migration_operations import InstallRecordInfrastructure, build_infrastructure_specification
from skeleton.common.models import OrganizationRecord, SealableMixin
from skeleton.records.sealing import seal_record
from skeleton.tenancy.context import tenant_context
from skeleton.tenancy.models import Organization
from skeleton.testing.sealing import assert_sealed_contract
from skeleton.testing.service_api import build_service_scenario
from tests.integration.sealing_support import sealed_document_table

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]
PASSWORD = "Sealing-kit-Password!33"
TABLE_SPEC = SimpleNamespace(scope="organization", sensitive_fields=(), personal_fields=())
DIGEST = hashlib.sha256(b"kit").hexdigest()


def probes(owner_schema_editor, *, with_seal=True):
    with isolate_apps("skeleton.system.apps.SystemConfig"):

        class KitDocument(SealableMixin, OrganizationRecord):
            organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="+")
            body = models.TextField(blank=True, default="")
            note = models.CharField(max_length=50, blank=True, default="")
            seal_fields = ("body",)

            class Meta(OrganizationRecord.Meta):
                app_label = "sk_system"
                db_table = "sk_test_kitdocument"

        class KitLine(OrganizationRecord):
            organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="+")
            document = models.ForeignKey(KitDocument, on_delete=models.PROTECT, related_name="+")
            text = models.TextField(blank=True, default="")
            seal_parent = "document"
            immutable_fields = ("document_id",)

            class Meta(OrganizationRecord.Meta):
                app_label = "sk_system"
                db_table = "sk_test_kitline"

        with owner_schema_editor() as editor:
            for model in (KitDocument, KitLine):
                editor.create_model(model)
                specification = build_infrastructure_specification(model, TABLE_SPEC)
                if not with_seal:
                    specification.pop("seal", None)
                    specification.pop("seal_parent", None)
                InstallRecordInfrastructure("sk_system", model._meta.model_name, specification)._postgres(editor, model)
        return KitDocument, KitLine


@pytest.fixture
def tenants():
    alice, alpha = bootstrap_identity(
        email="kit-alice@example.test", organization="kit-alpha", name="Alpha", password=PASSWORD
    )
    bob, beta = bootstrap_identity(
        email="kit-bob@example.test", organization="kit-beta", name="Beta", password=PASSWORD
    )
    return alice, alpha, bob, beta


def apply(record, actor):
    record.apply_seal(actor_id=actor.pk, digest=_digest(record), at=timezone.now())
    record.save(update_fields={"sealed_at", "sealed_by", "seal_digest"})
    return record


def _digest(record):
    from skeleton.common.sealing import digest
    from skeleton.records.sealing import seal_payload

    return digest(seal_payload(record))


def kit_arguments(document, line, tenants):
    alice, alpha, bob, beta = tenants
    return {
        "make_record": lambda: document.objects.create(organization=alpha, body="corpo"),
        "seal": lambda record: apply(record, alice),
        "acting": lambda: tenant_context(organization_id=alpha.pk, actor=alice, source="test:kit"),
        "foreign": lambda: tenant_context(organization_id=beta.pk, actor=bob, source="test:kit"),
        "make_child": lambda record: line.objects.create(organization=alpha, document=record, text="late"),
    }


def test_a_core_fixture_model_honours_the_contract(owner_schema_editor, tenants):
    document, line = probes(owner_schema_editor)
    assert_sealed_contract(document, **kit_arguments(document, line, tenants))


def test_the_kit_reports_a_guard_installed_without_the_seal(owner_schema_editor, tenants):
    document, line = probes(owner_schema_editor, with_seal=False)
    with pytest.raises(AssertionError) as failure:
        assert_sealed_contract(document, **kit_arguments(document, line, tenants))
    message = str(failure.value)
    assert "by SQL was not refused by the guard" in message


def test_a_test_model_of_the_example_app_honours_the_contract(owner_schema_editor):
    with sealed_document_table(owner_schema_editor) as model:
        scenario = build_service_scenario(model, slug="sealing-kit")

        def make_record():
            return model.objects.create(organization=scenario.a, owner=scenario.member, body="Assinado")

        def seal(record):
            return seal_record(record, user=scenario.member, expected_revision=record.revision)

        assert_sealed_contract(
            model,
            make_record=make_record,
            seal=seal,
            acting=scenario.as_member,
            foreign=lambda: scenario.acting(scenario.admin_b, scenario.b),
        )
