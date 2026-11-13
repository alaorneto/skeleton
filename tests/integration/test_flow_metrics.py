"""Flow figures on /metrics (#420): summed over organizations, labelled by flow key only."""

from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone
from prometheus_client.parser import text_string_to_metric_families

from skeleton.flows.models import FlowStepRun, FlowTask
from skeleton.tenancy.context import system_context
from tests.integration.test_flow_service_api import KEY, scenario, start
from tests.integration.test_metrics_endpoint import UUID_PATTERN

pytestmark = pytest.mark.needs_example_app
__all__ = ["scenario"]
TOKEN = "flow-metrics-token-0123456789abcdef"


def read(settings):
    settings.SKELETON_METRICS_TOKEN = TOKEN
    settings.SKELETON_METRICS_QUEUE_TTL_S = 0
    response = Client().get("/metrics", HTTP_AUTHORIZATION=f"Bearer {TOKEN}")
    assert response.status_code == 200, response.status_code
    found, labels = {}, []
    for family in text_string_to_metric_families(response.content.decode()):
        for sample in family.samples:
            if sample.name.startswith("skeleton_flow"):
                found[(sample.name, tuple(sorted(sample.labels.items())))] = sample.value
                labels.extend(sample.labels.values())
    return found, labels


def test_flow_figures_add_up_organizations_without_tenant_labels(scenario, settings):
    first = start(scenario)
    start(scenario, note=scenario.other)
    start(scenario, note=scenario.foreign, organization=scenario.b)
    with system_context(reason="test:flow-metrics", organization_id=scenario.a.pk):
        FlowTask.objects.filter(instance_id=first.sysid).update(due_at=timezone.now() - timedelta(hours=1))
        FlowStepRun.objects.create(
            organization_id=scenario.a.pk,
            instance_id=first.sysid,
            step_key="notify",
            kind="service",
            status="failed",
            sequence=999,
        )

    found, labels = read(settings)
    assert found[("skeleton_flow_metrics_up", ())] == 1
    assert found[("skeleton_flow_instances", (("flow_key", KEY), ("status", "running")))] == 3
    assert found[("skeleton_flow_tasks_open", (("flow_key", KEY),))] == 3
    assert found[("skeleton_flow_tasks_overdue", (("flow_key", KEY),))] == 1
    assert found[("skeleton_flow_failed_service_steps", (("flow_key", KEY),))] == 1
    forbidden = {str(item.pk) for item in (scenario.a, scenario.b, scenario.member, scenario.admin_a)}
    for value in labels:
        assert not UUID_PATTERN.search(value) and "@" not in value and value not in forbidden, value


def test_a_failed_read_reports_down_and_keeps_the_scrape(scenario, settings, monkeypatch):
    from skeleton.observability import flow_metrics

    def broken():
        raise RuntimeError("database away")

    monkeypatch.setattr(flow_metrics, "flow_snapshot", broken)
    found, _ = read(settings)
    assert found == {("skeleton_flow_metrics_up", ()): 0}
