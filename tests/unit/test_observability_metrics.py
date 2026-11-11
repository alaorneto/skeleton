"""Metrics registry, labels, token comparison and checks (#175), without a database."""

import hashlib
import hmac
from types import SimpleNamespace

import pytest
from django.core.exceptions import MiddlewareNotUsed
from prometheus_client.parser import text_string_to_metric_families

from skeleton.instance.schema import CATALOG
from skeleton.jobs.operations import QueueHealth
from skeleton.observability import checks, metrics

TOKEN = "metrics-token-for-tests-0123456789abcdef"


def test_bearer_is_compared_in_constant_time(monkeypatch):
    calls = []
    real = hmac.compare_digest

    def spy(left, right):
        calls.append((len(left), len(right)))
        return real(left, right)

    monkeypatch.setattr(metrics.hmac, "compare_digest", spy)
    assert metrics.bearer_matches(f"Bearer {TOKEN}", TOKEN)
    assert metrics.bearer_matches(f"bearer {TOKEN}", TOKEN)
    for header in ("", "Bearer", "Bearer x", f"Basic {TOKEN}", f"Bearer {TOKEN}0", TOKEN):
        assert not metrics.bearer_matches(header, TOKEN)
    assert not metrics.bearer_matches("Bearer ", "")
    # Every comparison ran, on digests of the same length whatever was presented.
    digest = hashlib.sha256().digest_size
    assert len(calls) == 9 and set(calls) == {(digest, digest)}


@pytest.mark.parametrize(
    "path,route,expected",
    [
        ("/o/0b1d8b2e-0000-4000-8000-000000000001/", "o/<uuid:organization_id>/", "o/<uuid:organization_id>/"),
        ("/missing/", None, "unresolved"),
        ("/health/ready/", None, "health/ready/"),
    ],
)
def test_route_label_is_the_pattern(path, route, expected):
    match = SimpleNamespace(route=route) if route else None
    assert metrics.route_label(SimpleNamespace(path_info=path, resolver_match=match)) == expected


def test_request_histogram_has_only_bounded_labels():
    histogram = metrics.request_histogram()
    assert histogram is metrics.request_histogram()
    assert histogram._labelnames == ("method", "route", "status")


def test_middleware_removes_itself_without_token(settings):
    settings.SKELETON_METRICS_TOKEN = ""
    with pytest.raises(MiddlewareNotUsed):
        metrics.MetricsMiddleware(lambda request: None)


def test_middleware_removes_itself_without_extra(settings, monkeypatch):
    settings.SKELETON_METRICS_TOKEN = TOKEN
    monkeypatch.setattr(metrics, "installed", lambda distributions: False)
    with pytest.raises(MiddlewareNotUsed):
        metrics.MetricsMiddleware(lambda request: None)


def test_queue_collector_groups_channels_and_unknown_types(monkeypatch):
    health = QueueHealth(due=3, oldest_due_lag_s=12.5, dead=4, live_workers=2, stale_workers=1)
    counts = {
        ("webhooks.deliver", "queued"): 2,
        ("notifications.email", "dead"): 4,
        ("notifications.push", "succeeded"): 1,
        ("removed.app.job", "queued"): 5,
        ("webhooks.deliver", "not-a-state"): 9,
    }
    monkeypatch.setattr(metrics, "queue_snapshot", lambda: (health, counts))
    from prometheus_client import generate_latest

    class One:
        def collect(self):
            return metrics.QueueCollector().collect()

    found = {
        (sample.name, tuple(sorted(sample.labels.items()))): sample.value
        for family in text_string_to_metric_families(generate_latest(One()).decode())
        for sample in family.samples
    }
    assert found[("skeleton_queue_metrics_up", ())] == 1
    assert found[("skeleton_delivery_jobs", (("channel", "webhook"), ("state", "queued")))] == 2
    assert found[("skeleton_delivery_jobs", (("channel", "email"), ("state", "dead")))] == 4
    assert found[("skeleton_delivery_jobs", (("channel", "push"), ("state", "succeeded")))] == 1
    assert found[("skeleton_jobs", (("job_type", "other"), ("state", "queued")))] == 5
    assert ("skeleton_jobs", (("job_type", "removed.app.job"), ("state", "queued"))) not in found
    assert found[("skeleton_jobs_due", ())] == 3
    assert found[("skeleton_jobs_oldest_due_lag_seconds", ())] == 12.5
    assert found[("skeleton_job_workers", (("state", "stale"),))] == 1


def test_queue_collector_survives_a_database_failure(monkeypatch):
    def broken():
        raise RuntimeError("database down")

    monkeypatch.setattr(metrics, "queue_snapshot", broken)
    families = list(metrics.QueueCollector().collect())
    assert [family.name for family in families] == ["skeleton_queue_metrics_up"]
    assert families[0].samples[0].value == 0


def test_checks_warn_without_extra_and_refuse_weak_tokens(settings, monkeypatch):
    settings.SKELETON_METRICS_TOKEN = ""
    assert checks.metrics_problems() == []
    settings.SKELETON_METRICS_TOKEN = TOKEN
    assert checks.metrics_problems() == []
    monkeypatch.setattr(checks, "installed", lambda distributions: False)
    assert [problem.id for problem in checks.metrics_problems()] == ["skeleton.W030"]
    monkeypatch.setattr(checks, "installed", lambda distributions: True)
    settings.MIDDLEWARE = [item for item in settings.MIDDLEWARE if item != metrics.MIDDLEWARE]
    assert [problem.id for problem in checks.metrics_problems()] == ["skeleton.W030"]
    settings.MIDDLEWARE = [*settings.MIDDLEWARE, metrics.MIDDLEWARE]
    settings.SKELETON_METRICS_TOKEN = "short"
    assert [problem.id for problem in checks.metrics_problems()] == ["skeleton.E030"]


def test_metrics_token_is_a_catalogued_secret():
    spec = CATALOG["metrics_token"]
    assert spec.env == "SKELETON_METRICS_TOKEN" and spec.secret
    assert spec.clean(TOKEN) == TOKEN
    for weak in ("short", "x" * 31, "a" * 20 + " " + "b" * 20):
        with pytest.raises(ValueError):
            spec.clean(weak)
