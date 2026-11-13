"""Process flow figures for ``/metrics`` (#420), read at scrape time.

The flow tables are tenant tables (RLS even in a system context), so the scrape reads each
active organization in its own system context and adds the results up; the figures are cached
for ``SKELETON_METRICS_QUEUE_TTL_S`` like the queue ones. Labels carry only the flow key:
never an organization or person sysid, a slug or an email. Failed automatic steps are read from
the database like everything else here, never counted in a worker.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Iterator
from typing import Any

from django.conf import settings

from skeleton.common.logging import security_event

MAX_ORGANIZATIONS = 2000
_cache: tuple[float, Any] | None = None


def flow_snapshot() -> dict[str, Counter]:
    from django.db.models import Count, Q
    from django.utils import timezone

    from skeleton.flows.models import FlowInstance, FlowStepRun, FlowTask
    from skeleton.tenancy.context import system_context
    from skeleton.tenancy.models import Organization

    now = timezone.now()
    totals: dict[str, Counter] = {
        "instances": Counter(),
        "open": Counter(),
        "overdue": Counter(),
        "failed_steps": Counter(),
    }
    with system_context(reason="metrics:flows"):
        organizations = list(
            Organization.all_objects.filter(is_active=True, deleted_at__isnull=True)
            .order_by("sysid")
            .values_list("pk", flat=True)[:MAX_ORGANIZATIONS]
        )
    for organization_id in organizations:
        with system_context(reason="metrics:flows", organization_id=organization_id):
            for row in FlowInstance.objects.values("flow_key", "status").annotate(total=Count("pk")):
                totals["instances"][(row["flow_key"], row["status"])] += row["total"]
            for row in (
                FlowTask.objects.filter(status="open")
                .values("instance__flow_key")
                .annotate(total=Count("pk"), late=Count("pk", filter=Q(due_at__lt=now)))
            ):
                totals["open"][row["instance__flow_key"]] += row["total"]
                totals["overdue"][row["instance__flow_key"]] += row["late"]
            for row in (
                FlowStepRun.objects.filter(status="failed", kind="service")
                .values("instance__flow_key")
                .annotate(total=Count("pk"))
            ):
                totals["failed_steps"][row["instance__flow_key"]] += row["total"]
    return totals


def cached_flow_snapshot() -> dict[str, Counter]:
    global _cache
    ttl = float(getattr(settings, "SKELETON_METRICS_QUEUE_TTL_S", 0) or 0)
    now = time.monotonic()
    if ttl > 0 and _cache is not None and now - _cache[0] < ttl:
        cached: dict[str, Counter] = _cache[1]
        return cached
    snapshot = flow_snapshot()
    _cache = (now, snapshot) if ttl > 0 else None
    return snapshot


class FlowCollector:
    def collect(self) -> Iterator[Any]:
        from prometheus_client.core import GaugeMetricFamily

        up = GaugeMetricFamily("skeleton_flow_metrics_up", "1 when this scrape read the flow tables, 0 when it failed.")
        try:
            snapshot = cached_flow_snapshot()
        except Exception:  # noqa: BLE001 - a database outage must not hide the other metrics
            security_event(
                "metrics.flows_unavailable",
                level=logging.WARNING,
                reason_code="flows_unavailable",
                source="metrics",
                exc_info=True,
            )
            up.add_metric([], 0)
            yield up
            return
        up.add_metric([], 1)
        yield up
        instances = GaugeMetricFamily(
            "skeleton_flow_instances", "Flow instances by flow and state.", labels=("flow_key", "status")
        )
        for (flow_key, status), total in sorted(snapshot["instances"].items()):
            instances.add_metric([flow_key, status], total)
        yield instances
        for name, key, text in (
            ("skeleton_flow_tasks_open", "open", "Open flow tasks by flow."),
            ("skeleton_flow_tasks_overdue", "overdue", "Open flow tasks past their deadline, by flow."),
            ("skeleton_flow_failed_service_steps", "failed_steps", "Automatic steps that failed, by flow."),
        ):
            family = GaugeMetricFamily(name, text, labels=("flow_key",))
            for flow_key, total in sorted(snapshot[key].items()):
                family.add_metric([flow_key], total)
            yield family
