"""Flow figures on /metrics (#420, #914): precomputed outside the scrape, labelled by registered flow key only."""

from datetime import timedelta

import pytest
from django.core.cache import cache
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from prometheus_client.parser import text_string_to_metric_families

from skeleton.flows.models import FlowInstance, FlowStepRun, FlowTask
from skeleton.jobs.services import enqueue
from skeleton.jobs.worker import Worker
from skeleton.observability import flow_metrics
from skeleton.tenancy.context import system_context
from skeleton.tenancy.models import Organization
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


def _run_periodic_flow_jobs(*organizations):
    for organization in organizations:
        with system_context(reason="test:flow-metrics-enqueue", organization_id=organization.pk):
            enqueue(flow_metrics.FLOW_ORG_JOB, organization_id=organization.pk)
    with system_context(reason="test:flow-metrics-enqueue"):
        enqueue(flow_metrics.FLOW_SNAPSHOT_JOB, organization_id=None)
    worker = Worker(worker_id="test-flow-metrics")
    while worker.run_once() is not None:
        pass


def test_flow_figures_are_precomputed_and_read_in_constant_queries(scenario, settings):
    cache.clear()
    flow_metrics.clear_flow_cache()
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

    _run_periodic_flow_jobs(scenario.a, scenario.b)
    with CaptureQueriesContext(connection) as two_orgs:
        found, labels = read(settings)
    assert found[("skeleton_flow_metrics_up", ())] == 1
    assert found[("skeleton_flow_instances", (("flow_key", KEY), ("status", "running")))] == 3
    assert found[("skeleton_flow_tasks_open", (("flow_key", KEY),))] == 3
    assert found[("skeleton_flow_tasks_overdue", (("flow_key", KEY),))] == 1
    assert found[("skeleton_flow_failed_service_steps", (("flow_key", KEY),))] == 1
    forbidden = {str(item.pk) for item in (scenario.a, scenario.b, scenario.member, scenario.admin_a)}
    for value in labels:
        assert not UUID_PATTERN.search(value) and "@" not in value and value not in forbidden, value

    # Adding more active organizations must not increase the number of SQL queries during GET /metrics (#914).
    with system_context(reason="test:flow-metrics-extra-orgs"):
        for index in range(3):
            Organization.all_objects.create(name=f"Extra {index}", slug=f"extra-flow-org-{index}", is_active=True)
    flow_metrics.refresh_flow_snapshot()
    with CaptureQueriesContext(connection) as five_orgs:
        read(settings)
    assert len(five_orgs) == len(two_orgs)

    # HEAD /metrics short-circuits before running any collector.
    with CaptureQueriesContext(connection) as head_queries:
        head = Client().head("/metrics", HTTP_AUTHORIZATION=f"Bearer {TOKEN}")
    assert head.status_code == 200 and head.content == b"" and len(head_queries) == 0


def test_unknown_flow_key_is_grouped_as_other(scenario, settings):
    """#914: flow keys outside the code registry never become Prometheus labels."""
    cache.clear()
    flow_metrics.clear_flow_cache()
    secret_key = "acme_tenant.private_approval"
    existing = start(scenario)
    with system_context(reason="test:flow-metrics-unknown", organization_id=scenario.a.pk):
        base = FlowInstance.objects.get(pk=existing.sysid)
        FlowInstance.objects.create(
            organization_id=scenario.a.pk,
            definition=base.definition,
            flow_key=secret_key,
            flow_version=1,
            subject_table=base.subject_table,
            subject_sysid=scenario.other.pk,
            status=FlowInstance.Status.RUNNING,
        )
    flow_metrics.refresh_flow_snapshot()
    found, labels = read(settings)
    assert found[("skeleton_flow_metrics_up", ())] == 1
    assert found[("skeleton_flow_instances", (("flow_key", KEY), ("status", "running")))] == 1
    assert found[("skeleton_flow_instances", (("flow_key", flow_metrics.OTHER_FLOW_KEY), ("status", "running")))] == 1
    assert secret_key not in labels


def test_missing_or_failed_snapshot_reports_down_and_keeps_the_scrape(scenario, settings, monkeypatch):
    cache.clear()
    flow_metrics.clear_flow_cache()
    found, _ = read(settings)
    assert found == {("skeleton_flow_metrics_up", ()): 0}

    def broken():
        raise RuntimeError("cache away")

    monkeypatch.setattr(flow_metrics, "read_flow_snapshot", broken)
    found, _ = read(settings)
    assert found == {("skeleton_flow_metrics_up", ()): 0}
