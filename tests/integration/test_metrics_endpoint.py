"""/metrics (#175): absent without the token, Bearer-only, queue figures equal to queue_health, no tenant labels."""

import re
from datetime import timedelta
from uuid import uuid4

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from prometheus_client.parser import text_string_to_metric_families

from skeleton.accounts.services import bootstrap_identity
from skeleton.integrations.webhooks.delivery import DELIVER_JOB
from skeleton.jobs import registry
from skeleton.jobs.models import Job, WorkerHeartbeat
from skeleton.jobs.operations import queue_health
from skeleton.jobs.registry import JobType, register_job_type
from skeleton.jobs.services import enqueue
from skeleton.tenancy.context import system_context

pytestmark = pytest.mark.usefixtures("skeleton_db")
TOKEN = "metrics-token-for-tests-0123456789abcdef"
PASSWORD = "Metrics-Password!175"
EMAIL = "metrics-person@example.test"
SLUG = "metrics-org"
UUID_PATTERN = re.compile(r"[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}", re.IGNORECASE)


def noop(**kwargs):
    return None


@pytest.fixture(autouse=True)
def job_types(monkeypatch):
    monkeypatch.setattr(registry, "_registry", dict(registry._registry))
    monkeypatch.setattr(registry, "_duplicates", [])
    register_job_type(JobType("test.metrics", noop, {}, max_attempts=2))


@pytest.fixture
def enabled(settings):
    settings.SKELETON_METRICS_TOKEN = TOKEN


@pytest.fixture
def identity():
    return bootstrap_identity(email=EMAIL, organization=SLUG, name="Metrics Person", password=PASSWORD)


def scrape(client=None, token=TOKEN):
    return (client or Client()).get("/metrics", HTTP_AUTHORIZATION=f"Bearer {token}")


def samples(response):
    assert response.status_code == 200, response.status_code
    found = {}
    for family in text_string_to_metric_families(response.content.decode()):
        for sample in family.samples:
            found[(sample.name, tuple(sorted(sample.labels.items())))] = sample.value
    return found


def test_metrics_do_not_exist_without_the_token(settings):
    settings.SKELETON_METRICS_TOKEN = ""
    response = Client().get("/metrics", HTTP_AUTHORIZATION=f"Bearer {TOKEN}")
    assert response.status_code == 404
    assert b"skeleton_jobs" not in response.content


@pytest.mark.parametrize(
    "header",
    ["", "Bearer", "Bearer wrong-token", f"Basic {TOKEN}", f"bearer {TOKEN}x", f"Token {TOKEN}"],
)
def test_wrong_or_missing_token_is_401_without_detail(enabled, header):
    extra = {"HTTP_AUTHORIZATION": header} if header else {}
    response = Client().get("/metrics", **extra)
    assert response.status_code == 401
    assert response.content == b""
    assert response["WWW-Authenticate"] == "Bearer"
    assert response["Cache-Control"] == "no-store"


def test_scrape_needs_get_or_head(enabled):
    assert Client().post("/metrics", HTTP_AUTHORIZATION=f"Bearer {TOKEN}").status_code == 405
    head = Client().head("/metrics", HTTP_AUTHORIZATION=f"Bearer {TOKEN}")
    assert head.status_code == 200 and head.content == b""


def test_scrape_answers_outside_session_and_tenant(enabled):
    response = scrape()
    assert response["Content-Type"].startswith("text/plain")
    assert "Set-Cookie" not in response and not response.cookies
    assert response["X-Request-ID"]


def test_queue_metrics_match_queue_health(enabled, identity):
    user, organization = identity
    now = timezone.now()
    with system_context(reason="test:metrics", organization_id=organization.pk):
        late = enqueue("test.metrics", organization_id=organization.pk)
        dead = enqueue("test.metrics", organization_id=organization.pk)
        future = enqueue("test.metrics", organization_id=organization.pk, run_after=now + timedelta(hours=1))
        delivery = enqueue(
            DELIVER_JOB, organization_id=organization.pk, actor=user, payload={"delivery_id": str(uuid4())}
        )
    Job.all_objects.filter(pk=late.id).update(run_after=now - timedelta(minutes=5))
    Job.all_objects.filter(pk=dead.id).update(state="dead", attempts=2)
    Job.all_objects.filter(pk=delivery.id).update(state="dead", attempts=3)
    WorkerHeartbeat(worker_id="metrics-live", state="running", last_seen=now).save()
    WorkerHeartbeat(worker_id="metrics-stale", state="running", last_seen=now - timedelta(hours=1)).save()

    found = samples(scrape())
    health = queue_health()
    assert found[("skeleton_queue_metrics_up", ())] == 1
    assert found[("skeleton_jobs_due", ())] == health.due == 1
    assert found[("skeleton_jobs_dead", ())] == health.dead == 2
    assert found[("skeleton_jobs_oldest_due_lag_seconds", ())] == pytest.approx(health.oldest_due_lag_s, abs=5)
    assert health.oldest_due_lag_s >= 290
    assert found[("skeleton_job_workers", (("state", "live"),))] == health.live_workers == 1
    assert found[("skeleton_job_workers", (("state", "stale"),))] == health.stale_workers == 1
    assert found[("skeleton_jobs", (("job_type", "test.metrics"), ("state", "queued")))] == 2
    assert found[("skeleton_jobs", (("job_type", "test.metrics"), ("state", "dead")))] == 1
    assert found[("skeleton_delivery_jobs", (("channel", "webhook"), ("state", "dead")))] == 1
    assert found[("skeleton_delivery_jobs", (("channel", "email"), ("state", "queued")))] == 0
    assert future.id != late.id


def test_request_metrics_label_routes_not_values(enabled, identity):
    user, organization = identity
    client = Client()
    assert client.post(reverse("account_login"), {"login": EMAIL, "password": PASSWORD}).status_code == 302
    client.get(f"/o/{organization.pk}/?email={EMAIL}&org={SLUG}")
    client.get(f"/o/{uuid4()}/catalog/")
    client.get(f"/no-such-page/{SLUG}/{organization.pk}/")
    client.get("/health/live/")

    found = samples(scrape())
    routes = {dict(labels).get("route") for name, labels in found if name.startswith("skeleton_http_request")}
    assert {"o/<uuid:organization_id>/", "o/<uuid:organization_id>/catalog/", "unresolved", "health/live/"} <= routes
    count = found[
        (
            "skeleton_http_request_duration_seconds_count",
            (("method", "GET"), ("route", "o/<uuid:organization_id>/catalog/"), ("status", "404")),
        )
    ]
    assert count >= 1


def test_no_label_carries_a_sysid_slug_or_email(enabled, identity):
    """Security negative: every label value of every series is scanned."""
    user, organization = identity
    client = Client()
    client.post(reverse("account_login"), {"login": EMAIL, "password": PASSWORD})
    for path in (f"/o/{organization.pk}/", f"/o/{organization.pk}/audit/", f"/{SLUG}/", f"/{EMAIL}/"):
        client.get(path)
    with system_context(reason="test:metrics", organization_id=organization.pk):
        enqueue("test.metrics", organization_id=organization.pk)

    text = scrape().content.decode()
    values = [
        value
        for family in text_string_to_metric_families(text)
        for sample in family.samples
        for value in sample.labels.values()
    ]
    assert values, "the scrape must have labelled series"
    forbidden = {str(organization.pk), str(user.pk), SLUG, EMAIL, organization.pk.hex, user.pk.hex}
    for value in values:
        assert not UUID_PATTERN.search(value), value
        assert "@" not in value, value
        assert not any(item in value for item in forbidden), value
    assert not any(item in text for item in forbidden)
