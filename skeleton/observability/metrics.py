"""Prometheus metrics at ``/metrics`` (#175), behind ``SKELETON_METRICS_TOKEN``.

Without the token (or without the ``[metrics]`` extra) the middleware removes itself, nothing
imports ``prometheus_client`` and ``/metrics`` is an ordinary 404. With the token, the endpoint
answers before the session, authentication and tenant middleware, and only to
``Authorization: Bearer <token>``, compared in constant time.

Labels never carry an organization or person sysid, a slug or an email: requests are labelled by
the resolved route *pattern*, the method and the status; jobs by their registered type and state.
Queue figures are read at scrape time from :func:`skeleton.jobs.operations.queue_health` and the
per-type counts, never counted in the worker.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from collections.abc import Iterator
from typing import Any

from django.conf import settings
from django.core.exceptions import MiddlewareNotUsed
from django.http import HttpResponse, HttpResponseNotAllowed

from skeleton.common.logging import security_event

from . import METRICS_DISTRIBUTIONS, installed

PATH = "/metrics"
MIDDLEWARE = "skeleton.observability.metrics.MetricsMiddleware"
METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
UNRESOLVED_ROUTE = "unresolved"
# Probes answer before URL resolution; they keep a fixed label of their own.
PROBE_ROUTES = {"/health/live/": "health/live/", "/health/ready/": "health/ready/"}
OTHER_JOB_TYPE = "other"
BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

_requests: Any = None


def metrics_token() -> str:
    return getattr(settings, "SKELETON_METRICS_TOKEN", "") or ""


def metrics_enabled() -> bool:
    return bool(metrics_token()) and installed(METRICS_DISTRIBUTIONS)


def request_histogram() -> Any:
    """Request latency by method, route pattern and status; created once per process.

    It is not registered in ``prometheus_client.REGISTRY``: the scrape below reads it directly
    (single process) or through the multiprocess files (``PROMETHEUS_MULTIPROC_DIR``).
    """
    global _requests
    if _requests is None:
        from prometheus_client import Histogram

        _requests = Histogram(
            "skeleton_http_request_duration_seconds",
            "HTTP request latency by method, resolved route pattern and status.",
            ("method", "route", "status"),
            buckets=BUCKETS,
            registry=None,
        )
    return _requests


def route_label(request: Any) -> str:
    """The URL pattern that resolved, never the path: ``orgs/<uuid:organization>/`` not its value."""
    if request.path_info in PROBE_ROUTES:
        return PROBE_ROUTES[request.path_info]
    match = getattr(request, "resolver_match", None)
    if match is None:
        # A middleware (tenant, maintenance) answered before URL resolution: resolve the pattern here.
        from django.urls import Resolver404, resolve

        try:
            match = resolve(request.path_info, getattr(request, "urlconf", None))
        except Resolver404:
            return UNRESOLVED_ROUTE
    route = getattr(match, "route", None)
    return str(route)[:200] if route else UNRESOLVED_ROUTE


def bearer_matches(header: str, token: str) -> bool:
    """Constant time: both sides are hashed to the same length before ``compare_digest``."""
    scheme, _, presented = (header or "").partition(" ")
    if scheme.lower() != "bearer":
        presented = ""
    same = hmac.compare_digest(
        hashlib.sha256(presented.strip().encode()).digest(), hashlib.sha256(token.encode()).digest()
    )
    return same and bool(token) and scheme.lower() == "bearer"


def _job_labels() -> tuple[tuple[str, ...], tuple[str, ...], dict[str, str]]:
    from skeleton.integrations.webhooks.delivery import DELIVER_JOB
    from skeleton.jobs.models import Job
    from skeleton.jobs.registry import all_job_types
    from skeleton.notifications.channels.delivery import EMAIL_JOB, PUSH_JOB

    types = tuple(job_type.code for job_type in all_job_types())
    channels = {DELIVER_JOB: "webhook", EMAIL_JOB: "email", PUSH_JOB: "push"}
    return types, tuple(Job.State.values), channels


def queue_snapshot() -> tuple[Any, dict[tuple[str, str], int]]:
    """Aggregates only, read in a system context of no organization (the job tables are global)."""
    from skeleton.jobs.operations import job_type_state_counts, queue_health
    from skeleton.tenancy.context import system_context

    with system_context(reason="metrics:queue"):
        return queue_health(), job_type_state_counts()


class QueueCollector:
    """Job, webhook and notification delivery figures, computed on every scrape."""

    def collect(self) -> Iterator[Any]:
        from prometheus_client.core import GaugeMetricFamily

        up = GaugeMetricFamily("skeleton_queue_metrics_up", "1 when this scrape read the job queue, 0 when it failed.")
        try:
            health, counts = queue_snapshot()
        except Exception:  # noqa: BLE001 - a database outage must not hide the request metrics
            security_event(
                "metrics.queue_unavailable",
                level=logging.WARNING,
                reason_code="queue_unavailable",
                source="metrics",
                exc_info=True,
            )
            up.add_metric([], 0)
            yield up
            return
        up.add_metric([], 1)
        yield up
        types, states, channels = _job_labels()
        per_type = {(job_type, state): 0 for job_type in types for state in states}
        per_channel = {(channel, state): 0 for channel in channels.values() for state in states}
        for (job_type, state), total in counts.items():
            if state not in states:
                continue
            label = job_type if job_type in types else OTHER_JOB_TYPE
            per_type[(label, state)] = per_type.get((label, state), 0) + total
            if job_type in channels:
                per_channel[(channels[job_type], state)] += total
        jobs = GaugeMetricFamily("skeleton_jobs", "Jobs by registered type and state.", labels=("job_type", "state"))
        for (job_type, state), total in sorted(per_type.items()):
            jobs.add_metric([job_type, state], total)
        yield jobs
        deliveries = GaugeMetricFamily(
            "skeleton_delivery_jobs",
            "Delivery jobs by channel (webhook, email, push) and state.",
            labels=("channel", "state"),
        )
        for (channel, state), total in sorted(per_channel.items()):
            deliveries.add_metric([channel, state], total)
        yield deliveries
        yield GaugeMetricFamily("skeleton_jobs_due", "Queued jobs whose run_after has passed.", value=health.due)
        yield GaugeMetricFamily(
            "skeleton_jobs_oldest_due_lag_seconds",
            "How long the oldest due job has been waiting.",
            value=health.oldest_due_lag_s,
        )
        yield GaugeMetricFamily("skeleton_jobs_dead", "Jobs that exhausted their attempts.", value=health.dead)
        workers = GaugeMetricFamily("skeleton_job_workers", "Workers by heartbeat freshness.", labels=("state",))
        workers.add_metric(["live"], health.live_workers)
        workers.add_metric(["stale"], health.stale_workers)
        yield workers


class _Scrape:
    """What one scrape exposes: the request histogram(s) and the queue read now."""

    def collect(self) -> Iterator[Any]:
        if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
            from prometheus_client import CollectorRegistry
            from prometheus_client.multiprocess import MultiProcessCollector

            registry = CollectorRegistry()
            MultiProcessCollector(registry)
            yield from registry.collect()
        else:
            yield from request_histogram().collect()
        yield from QueueCollector().collect()


def metrics_response(request: Any) -> HttpResponse:
    request.get_host()  # Never bypasses ALLOWED_HOSTS.
    if not bearer_matches(request.META.get("HTTP_AUTHORIZATION", ""), metrics_token()):
        security_event("metrics.denied", level=logging.WARNING, status=401, outcome="access_denied", source="metrics")
        denied = HttpResponse(status=401)
        denied["WWW-Authenticate"] = "Bearer"
        denied["Cache-Control"] = "no-store"
        return denied
    if request.method not in {"GET", "HEAD"}:
        return HttpResponseNotAllowed(["GET", "HEAD"])
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    response = HttpResponse(generate_latest(_Scrape()), content_type=CONTENT_TYPE_LATEST)
    response["Cache-Control"] = "no-store"
    if request.method == "HEAD":
        response.content = b""
    return response


class MetricsMiddleware:
    """Serves ``/metrics`` and times every other request; absent without the token or the extra."""

    def __init__(self, get_response: Any) -> None:
        if not metrics_enabled():
            raise MiddlewareNotUsed
        self.get_response = get_response
        self.requests = request_histogram()

    def __call__(self, request: Any) -> Any:
        if request.path_info == PATH:
            return metrics_response(request)
        started = time.perf_counter()
        response = self.get_response(request)
        method = request.method if request.method in METHODS else "other"
        self.requests.labels(method, route_label(request), str(response.status_code)).observe(
            time.perf_counter() - started
        )
        return response
