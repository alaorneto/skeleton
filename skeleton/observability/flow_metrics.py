"""Process flow figures for ``/metrics`` (#420, #914), precomputed outside the scrape.

The flow tables are tenant tables (RLS even in a system context), so collecting them during
``GET /metrics`` would run up to 2,000 tenant transactions per scrape. Instead, periodic jobs
collect each organization in its own system context (``observability.flow_snapshot_org``) and
aggregate the cached per-organization figures into one shared cache entry
(``observability.flow_snapshot``), which ``FlowCollector`` reads in O(1) with zero SQL queries.

Labels carry only registered ``flow_key`` names declared in code; any unknown or
organization-authored key is grouped under ``other`` so tenant text never enters Prometheus.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from collections.abc import Iterator, Mapping
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.cache import cache

from skeleton.common.logging import security_event

MAX_ORGANIZATIONS = 2000
OTHER_FLOW_KEY = "other"
FLOW_ORG_JOB = "observability.flow_snapshot_org"
FLOW_SNAPSHOT_JOB = "observability.flow_snapshot"
FLOW_SNAPSHOT_CACHE_KEY = "skeleton:observability:flow_snapshot"
FLOW_ORG_CACHE_PREFIX = "skeleton:observability:flow_org:"
FLOW_SNAPSHOT_TTL_S = 15 * 60
FLOW_SNAPSHOT_EVERY = timedelta(minutes=5)

_cache: tuple[float, dict[str, Counter]] | None = None
_flow_lock = threading.Lock()


def _empty_snapshot() -> dict[str, Counter]:
    return {
        "instances": Counter(),
        "open": Counter(),
        "overdue": Counter(),
        "failed_steps": Counter(),
    }


def _registered_flow_keys() -> frozenset[str]:
    from skeleton.flows.registry import all_flows

    return frozenset(spec.key for spec in all_flows())


def _label_flow_key(flow_key: str, registered: frozenset[str]) -> str:
    return flow_key if flow_key in registered else OTHER_FLOW_KEY


def _org_cache_key(organization_id: Any) -> str:
    return f"{FLOW_ORG_CACHE_PREFIX}{organization_id}"


def _serialize_snapshot(snapshot: Mapping[str, Counter]) -> dict[str, list[Any]]:
    return {
        "instances": [[flow_key, status, int(total)] for (flow_key, status), total in snapshot["instances"].items()],
        "open": [[flow_key, int(total)] for flow_key, total in snapshot["open"].items()],
        "overdue": [[flow_key, int(total)] for flow_key, total in snapshot["overdue"].items()],
        "failed_steps": [[flow_key, int(total)] for flow_key, total in snapshot["failed_steps"].items()],
    }


def _deserialize_snapshot(raw: Mapping[str, Any], *, registered: frozenset[str] | None = None) -> dict[str, Counter]:
    allowed = _registered_flow_keys() if registered is None else registered
    totals = _empty_snapshot()
    for flow_key, status, total in raw.get("instances", ()):
        totals["instances"][(_label_flow_key(str(flow_key), allowed), str(status))] += int(total)
    for bucket in ("open", "overdue", "failed_steps"):
        for flow_key, total in raw.get(bucket, ()):
            totals[bucket][_label_flow_key(str(flow_key), allowed)] += int(total)
    return totals


def _collect_current_organization(
    totals: dict[str, Counter], *, now: Any, registered: frozenset[str]
) -> dict[str, Counter]:
    from django.db.models import Count, Q

    from skeleton.flows.models import FlowInstance, FlowStepRun, FlowTask

    for row in FlowInstance.objects.values("flow_key", "status").annotate(total=Count("pk")):
        key = _label_flow_key(row["flow_key"], registered)
        totals["instances"][(key, row["status"])] += row["total"]
    for row in (
        FlowTask.objects.filter(status="open")
        .values("instance__flow_key")
        .annotate(total=Count("pk"), late=Count("pk", filter=Q(due_at__lt=now)))
    ):
        key = _label_flow_key(row["instance__flow_key"], registered)
        totals["open"][key] += row["total"]
        totals["overdue"][key] += row["late"]
    for row in (
        FlowStepRun.objects.filter(status="failed", kind="service")
        .values("instance__flow_key")
        .annotate(total=Count("pk"))
    ):
        key = _label_flow_key(row["instance__flow_key"], registered)
        totals["failed_steps"][key] += row["total"]
    return totals


def flow_snapshot() -> dict[str, Counter]:
    """Compute the flow figures across active organizations (one top-level system context per tenant)."""
    from django.utils import timezone

    from skeleton.tenancy.context import system_context
    from skeleton.tenancy.models import Organization

    now = timezone.now()
    registered = _registered_flow_keys()
    totals = _empty_snapshot()
    with system_context(reason="metrics:flows"):
        organizations = list(
            Organization.all_objects.filter(is_active=True, deleted_at__isnull=True)
            .order_by("sysid")
            .values_list("pk", flat=True)[:MAX_ORGANIZATIONS]
        )
    for organization_id in organizations:
        with system_context(reason="metrics:flows", organization_id=organization_id):
            _collect_current_organization(totals, now=now, registered=registered)
    return totals


def collect_organization_flows() -> dict[str, Counter]:
    """Per-organization periodic job handler: collects one organization into the shared cache."""
    from django.utils import timezone

    from skeleton.tenancy.context import current_context

    organization_id = current_context().organization_id
    totals = _empty_snapshot()
    if organization_id is None:
        return totals
    registered = _registered_flow_keys()
    _collect_current_organization(totals, now=timezone.now(), registered=registered)
    cache.set(_org_cache_key(organization_id), _serialize_snapshot(totals), timeout=FLOW_SNAPSHOT_TTL_S)
    return totals


def aggregate_flow_snapshot() -> dict[str, Counter]:
    """Global periodic job handler: sums the cached per-organization flow snapshots in O(1) SQL queries."""
    from skeleton.tenancy.models import Organization

    registered = _registered_flow_keys()
    organizations = list(
        Organization.all_objects.filter(is_active=True, deleted_at__isnull=True)
        .order_by("sysid")
        .values_list("pk", flat=True)[:MAX_ORGANIZATIONS]
    )
    totals = _empty_snapshot()
    if organizations:
        per_org = cache.get_many([_org_cache_key(organization_id) for organization_id in organizations])
        for raw in per_org.values():
            if not isinstance(raw, Mapping):
                continue
            part = _deserialize_snapshot(raw, registered=registered)
            for bucket, counter in part.items():
                totals[bucket].update(counter)
    cache.set(FLOW_SNAPSHOT_CACHE_KEY, _serialize_snapshot(totals), timeout=FLOW_SNAPSHOT_TTL_S)
    clear_flow_cache()
    return totals


def refresh_flow_snapshot() -> dict[str, Counter]:
    """Compute ``flow_snapshot()`` outside a context and publish it for ``/metrics`` scrapes."""
    snapshot = flow_snapshot()
    cache.set(FLOW_SNAPSHOT_CACHE_KEY, _serialize_snapshot(snapshot), timeout=FLOW_SNAPSHOT_TTL_S)
    clear_flow_cache()
    return snapshot


def clear_flow_cache() -> None:
    """Clear the process-local TTL cache for flow snapshots (used after refresh and in tests)."""
    global _cache
    with _flow_lock:
        _cache = None


def read_flow_snapshot() -> dict[str, Counter] | None:
    """Read the last precomputed snapshot from the shared cache (0 SQL queries)."""
    raw = cache.get(FLOW_SNAPSHOT_CACHE_KEY)
    if not isinstance(raw, Mapping):
        return None
    return _deserialize_snapshot(raw)


def cached_flow_snapshot() -> dict[str, Counter] | None:
    """Read the precomputed snapshot, reused per process up to ``SKELETON_METRICS_QUEUE_TTL_S``."""
    global _cache
    ttl = float(getattr(settings, "SKELETON_METRICS_QUEUE_TTL_S", 0) or 0)
    now = time.monotonic()
    if ttl > 0 and _cache is not None and now - _cache[0] < ttl:
        return _cache[1]
    with _flow_lock:
        now = time.monotonic()
        if ttl > 0 and _cache is not None and now - _cache[0] < ttl:
            return _cache[1]
        snapshot = read_flow_snapshot()
        _cache = (now, snapshot) if (ttl > 0 and snapshot is not None) else None
        return snapshot


def register_flow_metrics_jobs() -> None:
    """Register the per-organization collector and global aggregator jobs (#914)."""
    from skeleton.jobs.periodic import register_periodic
    from skeleton.jobs.registry import JobType, register_job_type

    register_job_type(
        JobType(
            code=FLOW_ORG_JOB,
            handler=collect_organization_flows,
            scope="organization",
            owner_app="sk_observability",
            max_attempts=2,
        )
    )
    register_job_type(
        JobType(
            code=FLOW_SNAPSHOT_JOB,
            handler=aggregate_flow_snapshot,
            scope="global",
            owner_app="sk_observability",
            max_attempts=2,
        )
    )
    register_periodic(FLOW_ORG_JOB, FLOW_ORG_JOB, every=FLOW_SNAPSHOT_EVERY, per_organization=True)
    register_periodic(FLOW_SNAPSHOT_JOB, FLOW_SNAPSHOT_JOB, every=FLOW_SNAPSHOT_EVERY, per_organization=False)


class FlowCollector:
    def collect(self) -> Iterator[Any]:
        from prometheus_client.core import GaugeMetricFamily

        up = GaugeMetricFamily(
            "skeleton_flow_metrics_up", "1 when a precomputed flow snapshot was available, 0 otherwise."
        )
        try:
            snapshot = cached_flow_snapshot()
        except Exception:  # noqa: BLE001 - a cache outage must not hide the other metrics
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
        if snapshot is None:
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
