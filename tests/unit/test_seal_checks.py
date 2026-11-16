"""E040 (#526, #528): sealable models and sealed parents declared coherently, frozen in the migration."""

from types import SimpleNamespace

import pytest
from django.db import models
from django.test.utils import isolate_apps

from skeleton.common.migration_operations import InstallRecordInfrastructure, build_infrastructure_specification
from skeleton.common.models import OrganizationRecord, SealableMixin
from skeleton.system.checks.extensions import seal_checks, seal_model_problems
from skeleton.tenancy.models import Organization


def table_spec(**values):
    return SimpleNamespace(
        scope="organization", sensitive_fields=(), personal_fields=(), write_fields=("body",), seal=object(), **values
    )


@pytest.fixture
def define():
    with isolate_apps("skeleton.system.apps.SystemConfig"):
        counter = iter(range(1000))

        def make(base=None, **attributes):
            name = f"Probe{next(counter)}"
            body = {
                "__module__": __name__,
                "organization": models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="+"),
                "body": models.TextField(default=""),
                "Meta": type("Meta", (OrganizationRecord.Meta,), {"app_label": "sk_system"}),
                **attributes,
            }
            bases = (SealableMixin, OrganizationRecord) if base is None else base
            return type(name, bases, body)

        yield make


def test_a_coherent_sealable_model_has_no_problem(define):
    model = define(seal_fields=("body",))
    operation = InstallRecordInfrastructure(
        "sk_system", "probe", build_infrastructure_specification(model, table_spec())
    )
    assert seal_model_problems(model, operation, table_spec()) == []


@pytest.mark.parametrize(
    ("seal_fields", "message"),
    [
        ((), "must declare seal_fields"),
        (("missing",), "not a concrete column"),
        (("sealed_at",), "system, access or seal column"),
        (("revision",), "system, access or seal column"),
        (("organization",), "system, access or seal column"),
    ],
)
def test_bad_seal_fields_are_reported(define, seal_fields, message):
    model = define(seal_fields=seal_fields)
    assert any(message in problem for problem in seal_model_problems(model))


def test_the_policy_and_the_write_fields_must_match_the_model(define):
    sealable = define(seal_fields=("body",))
    plain = define(base=(OrganizationRecord,))
    without_policy = SimpleNamespace(write_fields=(), seal=None)
    assert any("needs TableSpec.seal" in p for p in seal_model_problems(sealable, spec=without_policy))
    assert any("not sealable" in p for p in seal_model_problems(plain, spec=table_spec()))
    leaking = SimpleNamespace(write_fields=("body", "seal_digest"), seal=object())
    assert any("write_fields" in p for p in seal_model_problems(sealable, spec=leaking))


def test_a_model_diverging_from_the_frozen_specification_is_reported(define):
    model = define(seal_fields=("body",))
    stale = build_infrastructure_specification(model, table_spec())
    stale["seal"] = {**stale["seal"], "allow_delete": True}
    operation = InstallRecordInfrastructure("sk_system", "probe", stale)
    assert any("frozen migration specification" in p for p in seal_model_problems(model, operation, table_spec()))


def test_a_child_must_point_to_a_sealable_parent_of_the_same_app_with_an_immutable_key(define):
    parent = define(seal_fields=("body",))
    plain = define(base=(OrganizationRecord,))
    good = define(
        base=(OrganizationRecord,),
        document=models.ForeignKey(parent, on_delete=models.PROTECT, related_name="+"),
        seal_parent="document",
        immutable_fields=("document_id",),
    )
    assert seal_model_problems(good) == []
    mutable = define(
        base=(OrganizationRecord,),
        document=models.ForeignKey(parent, on_delete=models.PROTECT, related_name="+"),
        seal_parent="document",
    )
    assert any("immutable_fields" in p for p in seal_model_problems(mutable))
    unsealable = define(
        base=(OrganizationRecord,),
        document=models.ForeignKey(plain, on_delete=models.PROTECT, related_name="+"),
        seal_parent="document",
        immutable_fields=("document_id",),
    )
    assert any("sealable model of the same app" in p for p in seal_model_problems(unsealable))
    assert any(
        "sealable model" in p for p in seal_model_problems(define(base=(OrganizationRecord,), seal_parent="nope"))
    )


def test_the_specification_freezes_the_seal_and_the_parent(define):
    parent = define(seal_fields=("body",), seal_allows_delete=True, seal_condition_sql="NEW.body <> ''")
    child = define(
        base=(OrganizationRecord,),
        document=models.ForeignKey(parent, on_delete=models.PROTECT, related_name="+"),
        seal_parent="document",
        immutable_fields=("document_id",),
    )
    assert build_infrastructure_specification(parent, table_spec())["seal"] == {
        "fields": ["body"],
        "allow_delete": True,
        "condition_sql": "NEW.body <> ''",
    }
    assert build_infrastructure_specification(child, table_spec())["seal_parent"] == {
        "column": "document_id",
        "table": parent._meta.db_table,
    }
    assert "seal" not in build_infrastructure_specification(child, table_spec())


def test_the_core_has_no_seal_problem():
    assert seal_checks() == []
