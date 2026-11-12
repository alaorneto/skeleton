"""Signed webhook delivery with retries and automatic disablement (#64), against a local receiver."""

import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cryptography.fernet import Fernet
from django.test import override_settings
from django.utils import timezone

from skeleton.accounts.services import create_service_account
from skeleton.events import registry
from skeleton.events.registry import EventType, register_event_type
from skeleton.tenancy.context import system_context
from skeleton.testing.jobs import run_pending_jobs
from tools.security_fixtures import actor_tenant, build_security_fixture

pytestmark = pytest.mark.needs_example_app

KEY = Fernet.generate_key().decode()
EVENT = "notes.webhook_ready"


class Receiver(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.received.append({"headers": dict(self.headers), "body": body})
        status = self.server.statuses.pop(0) if self.server.statuses else 200
        self.send_response(status)
        answer = self.server.answer or (b"received " + str(status).encode() + b" \x00<b>")
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)


@pytest.fixture
def receiver():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    server.received, server.statuses, server.answer = [], [], b""
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def pipeline(skeleton_db, monkeypatch, receiver):
    from skeleton.access.models import MemberRole, Role
    from skeleton.integrations.webhooks.services import create_endpoint

    monkeypatch.setattr(registry, "_types", dict(registry._types))
    register_event_type(EventType(EVENT, "example_notes.note", ("code",), webhook=True))
    register_event_type(EventType("notes.quiet", "example_notes.note", ("code",), webhook=True))
    register_event_type(EventType("notes.internal_only", "example_notes.note", ("code",)))
    with override_settings(
        DEBUG=True, SKELETON_OUTBOUND_HTTP_DEVELOPMENT_OVERRIDE=True, INTEGRATION_ENCRYPTION_KEYS=[KEY]
    ):
        fixture = build_security_fixture()
        with actor_tenant(fixture.admin, fixture.a):
            principal = create_service_account(actor=fixture.admin, name="Receiver")
            membership = principal.organization_memberships.get()
            MemberRole(organization=fixture.a, membership=membership, role=Role.objects.get(code="member")).save()
            issued = create_endpoint(
                actor=fixture.admin,
                data={
                    "name": "Local",
                    "url": f"http://127.0.0.1:{receiver.server_port}/hook",
                    "principal": principal,
                    "event_types": [EVENT],
                },
            )
        yield fixture, principal, issued


def publish(fixture, note, event=EVENT):
    from skeleton.events.services import publish_event

    with actor_tenant(fixture.admin, fixture.a):
        publish_event(event, record=note, data={"code": note.code})


def drain():
    outcomes = []
    for _ in range(4):  # The consumer enqueues the delivery job, which may enqueue a retry.
        batch = run_pending_jobs()
        outcomes.extend(batch)
        if not batch:
            break
    return outcomes


def deliveries(fixture):
    from skeleton.integrations.models import WebhookDelivery

    with actor_tenant(fixture.admin, fixture.a):
        return list(WebhookDelivery.objects.order_by("created_at", "sysid"))


def due_now(fixture):
    """Move scheduled retries to now instead of waiting minutes."""
    from skeleton.jobs.models import Job

    with system_context(reason="test:webhook-due", organization_id=fixture.a.pk):
        Job.objects.filter(state="queued").update(run_after=timezone.now())


def verify(headers, body, secret):
    parts = dict(
        item.split("=", 1) for item in headers["Skeleton-Webhook-Signature"].split(",") if item.startswith("t=")
    )
    expected = hmac.new(secret.encode(), parts["t"].encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"v1={expected}" in headers["Skeleton-Webhook-Signature"].split(",")


def test_successful_delivery_is_signed_and_carries_identifiers_only(pipeline, receiver):
    fixture, principal, issued = pipeline
    publish(fixture, fixture.shared)
    drain()
    [request] = receiver.received
    body = json.loads(request["body"])
    assert set(body) == {"id", "type", "version", "occurred_at", "organization", "data"}
    assert body["type"] == EVENT and body["organization"] == str(fixture.a.pk)
    assert body["data"] == {"table": "example_notes.note", "record": str(fixture.shared.pk)}
    assert "secret body" not in request["body"].decode() and "shared" not in json.dumps(body["data"])
    assert verify(request["headers"], request["body"], issued.secret)
    [delivery] = deliveries(fixture)
    assert request["headers"]["Skeleton-Webhook-Id"] == str(delivery.pk)
    assert delivery.state == "succeeded" and delivery.attempts == 1 and delivery.status_code == 200
    assert "\x00" not in delivery.response_snippet
    # The consumer is idempotent: running the event again creates no second delivery.
    drain()
    assert len(deliveries(fixture)) == 1 and len(receiver.received) == 1


def test_server_errors_are_retried_on_schedule_and_client_errors_fail_at_once(pipeline, receiver):
    fixture, _, _ = pipeline
    receiver.statuses.extend([503, 200])
    publish(fixture, fixture.shared)
    drain()
    [retrying] = deliveries(fixture)
    assert retrying.state == "retrying" and retrying.attempts == 1 and retrying.error_code == "http_503"
    delay = retrying.next_attempt_at - retrying.last_attempt_at
    assert 55 <= delay.total_seconds() <= 65
    due_now(fixture)
    drain()
    [delivered] = deliveries(fixture)
    assert delivered.state == "succeeded" and delivered.attempts == 2
    from examples.notes.models import Note

    receiver.statuses.append(400)
    with actor_tenant(fixture.admin, fixture.a):
        second = Note(
            organization=fixture.a, owner=fixture.alice, code="second", title="Second", visibility="organization"
        )
        second.save()
    publish(fixture, second)
    drain()
    failed = [item for item in deliveries(fixture) if item.record_sysid == second.pk][0]
    assert failed.state == "failed" and failed.attempts == 1 and failed.error_code == "http_400"
    due_now(fixture)
    drain()
    assert (
        len([item for item in receiver.received if json.loads(item["body"])["data"]["record"] == str(second.pk)]) == 1
    )


def test_principal_without_read_access_sends_nothing(pipeline, receiver):
    fixture, _, _ = pipeline
    publish(fixture, fixture.private)  # Alice's private note: the service account cannot read it.
    drain()
    [delivery] = deliveries(fixture)
    assert delivery.state == "unauthorized" and delivery.attempts == 0
    assert receiver.received == []


def test_only_subscribed_webhook_events_create_deliveries(pipeline, receiver):
    fixture, _, _ = pipeline
    publish(fixture, fixture.shared, "notes.quiet")  # A webhook event the endpoint did not subscribe to.
    publish(fixture, fixture.shared, "notes.internal_only")  # Not exposed to webhooks at all.
    drain()
    assert deliveries(fixture) == [] and receiver.received == []


def test_rotation_sends_both_signatures(pipeline, receiver):
    from skeleton.integrations.webhooks.services import rotate_secret

    fixture, _, issued = pipeline
    with actor_tenant(fixture.admin, fixture.a):
        rotated = rotate_secret(issued.endpoint, actor=fixture.admin, expected_revision=issued.endpoint.revision)
    publish(fixture, fixture.shared)
    drain()
    [request] = receiver.received
    assert request["headers"]["Skeleton-Webhook-Signature"].count("v1=") == 2
    assert verify(request["headers"], request["body"], rotated.secret)
    assert verify(request["headers"], request["body"], issued.secret)


def test_fifteen_consecutive_failures_disable_the_endpoint_and_notify(pipeline, receiver):
    from examples.notes.models import Note
    from skeleton.integrations.models import WebhookEndpoint, WebhookEndpointHealth
    from skeleton.notifications.models import Notification

    fixture, _, issued = pipeline
    receiver.statuses.extend([400] * 15)
    notes = []
    with actor_tenant(fixture.admin, fixture.a):
        for index in range(16):
            note = Note(
                organization=fixture.a, owner=fixture.alice, code=f"n{index}", title="N", visibility="organization"
            )
            note.save()
            notes.append(note)
    for note in notes[:15]:
        publish(fixture, note)
    drain()
    with actor_tenant(fixture.admin, fixture.a):
        endpoint = WebhookEndpoint.objects.get(pk=issued.endpoint.pk)
        assert not endpoint.is_enabled and endpoint.disabled_reason == "consecutive_failures"
        assert WebhookEndpointHealth.objects.get(endpoint=endpoint).consecutive_failures == 15
        assert Notification.objects.filter(type_code="webhooks.endpoint_disabled").count() == 1
    publish(fixture, notes[15])
    drain()
    assert len(receiver.received) == 15
    assert all(item.record_sysid != notes[15].pk for item in deliveries(fixture))


# --------------------------------------------------------------------------- log, resend, ping, retention (#65)


def login(user):
    from django.test import Client
    from django.urls import reverse

    client = Client(enforce_csrf_checks=True)
    client.get(reverse("account_login"))
    response = client.post(
        reverse("account_login"),
        {
            "login": user.email,
            "password": "Fixture-only-password-2026!",
            "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
        },
    )
    assert response.status_code == 302
    return client


def api_post(client, url, revision=None):
    headers = {"HTTP_X_CSRFTOKEN": client.cookies["csrftoken"].value}
    if revision is not None:
        headers["HTTP_IF_MATCH"] = f'"{revision}"'
    return client.post(url, data="{}", content_type="application/json", **headers)


def urls(fixture, endpoint, delivery=None):
    from django.urls import reverse

    base = {"organization_id": fixture.a.pk, "sysid": endpoint.pk}
    result = {
        "page": reverse("integrations:webhook-deliveries", kwargs=base),
        "list": reverse("integrations:api-webhook-deliveries", kwargs=base),
        "ping": reverse("integrations:api-webhook-ping", kwargs=base),
    }
    if delivery is not None:
        item = {**base, "delivery_id": delivery.pk}
        result["detail"] = reverse("integrations:api-webhook-delivery", kwargs=item)
        result["resend"] = reverse("integrations:api-webhook-delivery-resend", kwargs=item)
        result["html_resend"] = reverse("integrations:webhook-delivery-resend", kwargs=item)
    return result


def test_log_shows_bounded_escaped_answers(pipeline, receiver):
    fixture, _, issued = pipeline
    receiver.answer = b"<script>alert(1)</script>" + b"y" * 4000
    publish(fixture, fixture.shared)
    drain()
    [delivery] = deliveries(fixture)
    assert len(delivery.response_snippet) == 1024 and delivery.response_snippet.startswith("<script>")
    client = login(fixture.admin)
    paths = urls(fixture, issued.endpoint, delivery)
    page = client.get(paths["page"]).content.decode()
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page and "<script>alert(1)" not in page
    listed = client.get(paths["list"]).json()
    assert listed["count"] == 1 and listed["results"][0]["state"] == "succeeded"
    assert len(listed["results"][0]["response_snippet"]) == 1024


def test_resend_creates_a_new_attempt_and_keeps_the_history(pipeline, receiver):
    from skeleton.integrations.models import WebhookEndpoint

    fixture, _, issued = pipeline
    receiver.statuses.append(400)
    publish(fixture, fixture.shared)
    drain()
    [original] = deliveries(fixture)
    assert original.state == "failed"
    client = login(fixture.admin)
    paths = urls(fixture, issued.endpoint, original)
    with actor_tenant(fixture.admin, fixture.a):
        revision = WebhookEndpoint.objects.get(pk=issued.endpoint.pk).revision
    assert api_post(client, paths["resend"]).status_code == 428
    assert api_post(client, paths["resend"], revision + 1).status_code == 412
    response = api_post(client, paths["resend"], revision)
    assert response.status_code == 202 and response.json()["resend_of"] == str(original.pk)
    drain()
    after = {item.pk: item for item in deliveries(fixture)}
    unchanged = after[original.pk]
    assert (unchanged.state, unchanged.attempts, unchanged.status_code) == ("failed", 1, 400)
    [retry] = [item for item in after.values() if item.pk != original.pk]
    assert retry.state == "succeeded" and retry.event_id == original.event_id and retry.payload == original.payload
    assert len(receiver.received) == 2
    assert receiver.received[0]["body"] == receiver.received[1]["body"]
    # The HTML form takes the same revision; a stale one changes nothing.
    token = client.cookies["csrftoken"].value
    html = client.post(paths["html_resend"], {"revision": revision - 1, "csrfmiddlewaretoken": token})
    assert html.status_code == 302 and len(deliveries(fixture)) == 2


def test_ping_sends_the_reserved_event_without_a_record(pipeline, receiver):
    fixture, _, issued = pipeline
    client = login(fixture.admin)
    response = api_post(client, urls(fixture, issued.endpoint)["ping"], issued.endpoint.revision)
    assert response.status_code == 202 and response.json()["event_type"] == "webhook.ping"
    drain()
    [request] = receiver.received
    body = json.loads(request["body"])
    assert body["type"] == "webhook.ping" and body["data"] == {}
    assert verify(request["headers"], request["body"], issued.secret)
    [ping] = deliveries(fixture)
    assert ping.state == "succeeded" and ping.record_sysid is None


def test_deliveries_of_another_organization_do_not_exist(pipeline, receiver):
    from django.urls import reverse

    fixture, _, issued = pipeline
    publish(fixture, fixture.shared)
    drain()
    [delivery] = deliveries(fixture)
    paths = urls(fixture, issued.endpoint, delivery)
    for user in (fixture.alice, fixture.bob):  # Member of A without the capability; B's administrator.
        client = login(user)
        for key in ("page", "list", "detail"):
            assert client.get(paths[key]).status_code == 404, (user.email, key)
        assert api_post(client, paths["resend"], issued.endpoint.revision).status_code == 404
    in_b = reverse(
        "integrations:api-webhook-delivery",
        kwargs={"organization_id": fixture.b.pk, "sysid": issued.endpoint.pk, "delivery_id": delivery.pk},
    )
    assert login(fixture.bob).get(in_b).status_code == 404
    assert len(deliveries(fixture)) == 1


def test_retention_deletes_only_expired_final_deliveries(pipeline, receiver):
    from datetime import timedelta

    from examples.notes.models import Note
    from skeleton.integrations.models import WebhookDelivery
    from skeleton.integrations.webhooks.log import purge_expired_deliveries, resend_delivery

    fixture, _, issued = pipeline
    receiver.statuses.extend([400, 400, 503])
    with actor_tenant(fixture.admin, fixture.a):
        notes = [
            Note(organization=fixture.a, owner=fixture.alice, code=f"r{index}", title="R", visibility="organization")
            for index in range(3)
        ]
        for note in notes:
            note.save()
    for note in notes:
        publish(fixture, note)
    drain()
    by_note = {item.record_sysid: item for item in deliveries(fixture)}
    old, referenced, retrying = by_note[notes[0].pk], by_note[notes[1].pk], by_note[notes[2].pk]
    assert (old.state, referenced.state, retrying.state) == ("failed", "failed", "retrying")
    with actor_tenant(fixture.admin, fixture.a):
        resend = resend_delivery(referenced, actor=fixture.admin, expected_revision=issued.endpoint.revision)
    boundary = timezone.now()
    later = boundary + timedelta(days=31)
    with system_context(reason="test:webhook-retention", organization_id=fixture.a.pk):
        # Deliveries are immutable in time: run retention 31 days later instead of backdating rows.
        assert purge_expired_deliveries(now=later, created_before=boundary) == 1
        remaining = set(WebhookDelivery.objects.values_list("pk", flat=True))
    # Not final yet, or the original of a live resend: both wait.
    assert remaining == {referenced.pk, retrying.pk, resend.pk}
