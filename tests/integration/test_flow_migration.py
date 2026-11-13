"""Migrate running instances to a new version of their flow (#413)."""

import json
from dataclasses import replace

import pytest
from django.urls import reverse

from skeleton.flows import registry as flows
from skeleton.flows.models import FlowInstance, FlowTask
from skeleton.tenancy.context import system_context
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.service_api import FIXTURE_PASSWORD
from tests import support
from tests.integration.test_flow_service_api import KEY, approval_spec, fill, scenario, start

pytestmark = pytest.mark.needs_example_app
__all__ = ["scenario"]


def version_two():
    v1 = approval_spec()
    steps = tuple(replace(step, key="draft") if step.key == "fill" else step for step in v1.steps)
    transitions = tuple(
        replace(
            item,
            source="draft" if item.source == "fill" else item.source,
            target="draft" if item.target == "fill" else item.target,
        )
        for item in v1.transitions
    )
    return replace(v1, version=2, label="Aprovação v2", steps=steps, transitions=transitions, _index={})


@pytest.fixture
def upgraded(scenario, monkeypatch):
    from skeleton.flows import registry

    at_fill = start(scenario)  # Its open step is "fill".
    at_review = start(scenario, note=scenario.other)
    fill(scenario)  # The first task of the member's inbox moves one instance to "review".
    theirs = start(scenario, note=scenario.foreign, organization=scenario.b)
    monkeypatch.setitem(registry._flows, KEY, version_two())
    monkeypatch.setattr(registry, "_migrations", {})
    flows.register_flow_migration(
        KEY, from_version=1, to_version=2, steps={"fill": "draft", "review": "review"}, owner_app="example_notes"
    )
    assert flows.flow_problems() == []
    return scenario, at_fill, at_review, theirs


def state(scenario, receipt, organization=None):
    with system_context(reason="test:migration", organization_id=(organization or scenario.a).pk):
        instance = FlowInstance.objects.get(pk=receipt.sysid)
        tasks = {row.step_key: (row.pk, row.status) for row in FlowTask.objects.filter(instance=instance)}
        return instance.flow_version, tasks


def test_preview_then_apply_moves_and_keeps_the_right_tasks(upgraded):
    scenario, *_ = upgraded
    admin = support.login(scenario.admin_a, password=FIXTURE_PASSWORD, csrf=False)
    url = reverse("flows:api-migration", args=[scenario.a.pk, KEY, 1])
    before = {receipt: state(scenario, receipt) for receipt in upgraded[1:3]}
    shown = admin.get(url)
    assert shown.status_code == 200, shown.content
    moves = {item["instance_id"]: item["moves"] for item in shown.json()["results"]}
    assert sorted(map(tuple, sum(moves.values(), []))) == [("fill", "draft"), ("review", "review")]
    assert {receipt: state(scenario, receipt) for receipt in upgraded[1:3]} == before  # A preview writes nothing.
    assert admin.post(url, data=json.dumps({}), content_type="application/json").status_code == 202
    run_pending_jobs()
    reviewed = [receipt for receipt in upgraded[1:3] if "review" in before[receipt][1]][0]
    filling = [receipt for receipt in upgraded[1:3] if receipt is not reviewed][0]
    version, tasks = state(scenario, reviewed)
    assert version == 2 and tasks["review"] == before[reviewed][1]["review"]  # Same step: the task stays.
    version, tasks = state(scenario, filling)
    assert version == 2 and tasks["fill"][1] == "cancelled" and tasks["draft"][1] == "open"
    assert state(scenario, upgraded[3], organization=scenario.b)[0] == 1  # Another organization: untouched.


def test_unmapped_instances_are_skipped_and_members_are_refused(upgraded, monkeypatch):
    from skeleton.flows import registry

    scenario, at_fill, *_ = upgraded
    monkeypatch.setattr(registry, "_migrations", {})
    flows.register_flow_migration(
        KEY, from_version=1, to_version=2, steps={"review": "review"}, owner_app="example_notes"
    )
    admin = support.login(scenario.admin_a, password=FIXTURE_PASSWORD, csrf=False)
    url = reverse("flows:api-migration", args=[scenario.a.pk, KEY, 1])
    skipped = {item["instance_id"]: item["skipped"] for item in admin.get(url).json()["results"]}
    assert skipped[str(at_fill.sysid)] == "unmapped:fill"
    admin.post(url, data=json.dumps({}), content_type="application/json")
    run_pending_jobs()
    assert state(scenario, at_fill)[0] == 1
    member = support.login(scenario.member, password=FIXTURE_PASSWORD, csrf=False)
    assert member.get(url).status_code == 403
    assert member.post(url, data=json.dumps({}), content_type="application/json").status_code == 403
    assert admin.get(reverse("flows:api-migration", args=[scenario.a.pk, KEY, 7])).status_code == 400


def test_the_check_refuses_a_migration_to_a_missing_step(scenario, monkeypatch):
    from skeleton.flows import registry

    monkeypatch.setattr(registry, "_migrations", {})
    monkeypatch.setitem(registry._flows, KEY, version_two())
    flows.register_flow_migration(
        KEY, from_version=1, to_version=2, steps={"fill": "nowhere"}, owner_app="example_notes"
    )
    assert any("not in v2" in item for item in flows.flow_problems())
