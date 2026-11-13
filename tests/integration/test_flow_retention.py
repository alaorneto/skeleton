"""Retention and erasure of flow form data (#422): purge of closed instances, personal fields, export."""

import io
import json
import zipfile
from dataclasses import replace
from datetime import timedelta

import pytest
from django.utils import timezone

from skeleton.accounts.privacy import export_my_data
from skeleton.flows import registry as flows
from skeleton.flows.models import FlowActivity, FlowInstance, FlowSubmission, FlowTask
from skeleton.flows.retention import PURGE_JOB, forget_person, purge_form_data
from skeleton.tenancy.context import system_context
from tests.integration import test_flow_service_api as base
from tests.integration.test_flow_service_api import call, fill, scenario, service, start

pytestmark = pytest.mark.needs_example_app
__all__ = ["scenario"]
FORM = flows.Form(
    flows.Field("summary", "str", label="Resumo", required=True, max_length=100),
    flows.Field("phone", "str", label="Telefone", max_length=30, personal=True),
    flows.Field("remark", "text", label="Reservado", sensitive=True, personal=True),
)


def retained_spec(days):
    spec = base.approval_spec()
    steps = tuple(replace(step, form=FORM) if step.key == "fill" else step for step in spec.steps)
    return replace(spec, steps=steps, retention_days=days)


@pytest.fixture
def retained(scenario, monkeypatch):
    from skeleton.flows import registry

    monkeypatch.setattr(registry, "_flows", {})
    flows.register_flow(retained_spec(30))
    assert flows.flow_problems() == []
    return scenario


def filled_and_cancelled(scenario, note=None, organization=None, **data):
    receipt = start(scenario, note=note, organization=organization)
    if organization is None:
        fill(scenario, **(data or {"summary": "Resumo", "phone": "+55 11 5555-0000", "remark": "reservado"}))
    current = call(
        scenario, lambda actor: service.instance(actor=actor, instance_id=receipt.sysid), organization=organization
    )
    call(
        scenario,
        lambda actor: service.cancel(actor=actor, instance_id=receipt.sysid, expected_revision=current.revision),
        organization=organization,
    )
    return receipt


def rows(organization, instance_id):
    with system_context(reason="test:flow-retention", organization_id=organization.pk):
        instance = FlowInstance.objects.get(pk=instance_id)
        submissions = list(FlowSubmission.objects.filter(instance=instance).order_by("created_at"))
        tasks = FlowTask.objects.filter(instance=instance).count()
        activities = FlowActivity.objects.filter(instance=instance).count()
    return instance, submissions, tasks, activities


def purge(organization, *, days):
    with system_context(reason="test:flow-retention", organization_id=organization.pk):
        return purge_form_data(now=timezone.now() + timedelta(days=days))


def test_a_closed_instance_past_retention_keeps_only_its_skeleton(retained):
    closed = filled_and_cancelled(retained)
    running = start(retained, note=retained.other)
    before, _, tasks, activities = rows(retained.a, closed.sysid)
    assert before.data["summary"] == "Resumo" and before.sensitive_data == {"remark": "reservado"}

    assert purge(retained.a, days=10) == 0  # Inside the window.
    assert purge(retained.a, days=31) == 1
    after, submissions, tasks_after, activities_after = rows(retained.a, closed.sysid)
    assert (after.data, after.sensitive_data, after.status) == ({}, {}, "cancelled")
    assert after.form_purged_at is not None and after.finished_at == before.finished_at
    assert all(item.values == {} and item.sensitive_values == {} for item in submissions)
    assert (tasks_after, activities_after) == (tasks, activities)
    untouched, *_ = rows(retained.a, running.sysid)
    assert untouched.form_purged_at is None and untouched.status == "running"
    assert purge(retained.a, days=31) == 0  # A second run changes nothing.


def test_the_purge_stays_in_its_organization_and_follows_the_setting(retained, settings, monkeypatch):
    from skeleton.flows import registry

    foreign = filled_and_cancelled(retained, note=retained.foreign, organization=retained.b)
    assert purge(retained.a, days=31) == 0
    assert rows(retained.b, foreign.sysid)[0].form_purged_at is None
    monkeypatch.setattr(registry, "_flows", {})
    flows.register_flow(retained_spec(0))  # Same version: the frozen definition still says 30.
    assert purge(retained.b, days=31) == 1
    assert PURGE_JOB == "flows.purge_form_data"


def test_without_retention_nothing_is_purged(scenario, settings):
    settings.SKELETON_FLOWS_FORM_RETENTION_DAYS = 0
    closed = filled_and_cancelled(scenario, summary="Resumo")
    assert purge(scenario.a, days=10000) == 0
    settings.SKELETON_FLOWS_FORM_RETENTION_DAYS = 5
    assert purge(scenario.a, days=6) == 1
    assert rows(scenario.a, closed.sysid)[0].data == {}


def test_erasure_removes_the_persons_personal_fields_and_export_lists_the_forms(retained):
    closed = filled_and_cancelled(retained)
    data = json.loads(zipfile.ZipFile(io.BytesIO(export_my_data(retained.member))).read("dados.json"))
    sent = [item for part in data["organizations"] for item in part["flow_submissions"]]
    assert {"summary": "Resumo", "phone": "+55 11 5555-0000"}.items() <= next(
        item["values"] for item in sent if item["step_key"] == "fill"
    ).items()
    assert all(item["instance_sysid"] == str(closed.sysid) for item in sent)
    admin_export = json.loads(zipfile.ZipFile(io.BytesIO(export_my_data(retained.admin_a))).read("dados.json"))
    assert all(not part["flow_submissions"] for part in admin_export["organizations"])  # Started by member.

    with system_context(reason="test:flow-retention", organization_id=retained.b.pk):
        assert forget_person(retained.member.pk) == 0  # Nothing of theirs in B.
    with system_context(reason="test:flow-retention", organization_id=retained.a.pk):
        assert forget_person(retained.member.pk) == 1
        assert forget_person(retained.member.pk) == 0
    instance, submissions, *_ = rows(retained.a, closed.sysid)
    filled = next(item for item in submissions if item.step_key == "fill")
    assert filled.values == {"summary": "Resumo"} and filled.sensitive_values == {}
    assert "phone" not in instance.data and instance.data["summary"] == "Resumo"
    assert "remark" not in instance.sensitive_data


def test_the_audit_never_copies_form_values(retained):
    from skeleton.audit.models import AuditEvent

    filled_and_cancelled(retained)
    with system_context(reason="test:flow-retention", organization_id=retained.a.pk):
        events = [
            event.changes
            for event in AuditEvent.objects.filter(table_name__startswith="sk_flows")
            if event.table_name.endswith(("flowinstance", "flowsubmission"))
        ]
    assert events
    text = json.dumps(events, ensure_ascii=False)
    assert "+55 11 5555-0000" not in text and "reservado" not in text and '"Resumo"' not in text
