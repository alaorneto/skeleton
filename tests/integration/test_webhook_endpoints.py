"""Webhook endpoints of an organization (#63): validation, secrets, capability and isolation."""

import json
import socket
from datetime import timedelta

import pytest
from cryptography.fernet import Fernet
from django.db import connection
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from skeleton.accounts.services import create_service_account
from skeleton.events.registry import EventType, register_event_type
from skeleton.testing.registries import isolated_event_types
from tests import support
from tools.security_fixtures import actor_tenant, build_security_fixture

pytestmark = pytest.mark.needs_example_app

PASSWORD = "Fixture-only-password-2026!"
KEY = Fernet.generate_key().decode()
ADDRESSES = {"hooks.example": "93.184.215.14", "internal.example": "10.0.0.7"}


def fake_getaddrinfo(host, port, *args, **kwargs):
    if host not in ADDRESSES:
        raise socket.gaierror("unknown host")
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ADDRESSES[host], port))]


@pytest.fixture
def webhooks(skeleton_db, monkeypatch):
    from skeleton.common import outbound_http

    monkeypatch.setattr(outbound_http.socket, "getaddrinfo", fake_getaddrinfo)
    with isolated_event_types(keep_consumers=True):
        register_event_type(EventType("notes.webhook_ready", "example_notes.note", ("code",), webhook=True))
        register_event_type(EventType("notes.internal_only", "example_notes.note", ("code",)))
        with override_settings(INTEGRATION_ENCRYPTION_KEYS=[KEY]):
            fixture = build_security_fixture()
            with actor_tenant(fixture.admin, fixture.a):
                principal = create_service_account(actor=fixture.admin, name="Receiver")
            with actor_tenant(fixture.bob, fixture.b):
                foreign = create_service_account(actor=fixture.bob, name="Foreign")
            yield fixture, principal, foreign


def login(user):
    return support.login(user, password=PASSWORD)


def api(client, method, url, data=None, revision=None):
    headers = {"HTTP_X_CSRFTOKEN": client.cookies["csrftoken"].value}
    if revision is not None:
        headers["HTTP_IF_MATCH"] = f'"{revision}"'
    return getattr(client, method)(url, data=json.dumps(data or {}), content_type="application/json", **headers)


def collection(organization):
    return reverse("integrations:api-webhooks", kwargs={"organization_id": organization.pk})


def detail(organization, sysid, name="integrations:api-webhook"):
    return reverse(name, kwargs={"organization_id": organization.pk, "sysid": sysid})


def payload(principal, **overrides):
    return {
        "name": "ERP",
        "url": "https://hooks.example/skeleton",
        "principal": str(principal.pk),
        "event_types": ["notes.webhook_ready"],
        **overrides,
    }


def test_create_returns_the_secret_once_and_never_stores_or_audits_it(webhooks):
    from skeleton.audit.models import AuditEvent
    from skeleton.integrations.models import WebhookEndpoint

    fixture, principal, _ = webhooks
    client = login(fixture.admin)
    created = api(client, "post", collection(fixture.a), payload(principal))
    assert created.status_code == 201, created.content
    body = created.json()
    secret = body["secret"]
    assert secret.startswith("whsec_") and "no-store" in created["Cache-Control"]
    read = client.get(detail(fixture.a, body["sysid"]))
    assert read.status_code == 200 and "secret" not in read.json() and secret not in read.content.decode()
    listing = client.get(collection(fixture.a))
    assert secret not in listing.content.decode() and listing.json()["count"] == 1
    page = client.get(reverse("integrations:webhook", kwargs={"organization_id": fixture.a.pk, "sysid": body["sysid"]}))
    assert page.status_code == 200 and secret not in page.content.decode()
    with actor_tenant(fixture.admin, fixture.a), connection.cursor() as cursor:  # RLS needs the tenant.
        cursor.execute(f"SELECT secret_ciphertext FROM {WebhookEndpoint._meta.db_table}")
        stored = [row[0] for row in cursor.fetchall()]
    assert stored and secret not in stored[0] and stored[0].startswith("gAAAA")
    with actor_tenant(fixture.admin, fixture.a):
        events = list(AuditEvent.objects.filter(record_sysid=body["sysid"]).values_list("changes", flat=True))
    assert events and secret not in json.dumps(events, default=str)
    assert "gAAAA" not in json.dumps(events, default=str)  # Not even the ciphertext reaches the audit trail.


@pytest.mark.parametrize(
    "overrides,field",
    [
        ({"url": "http://hooks.example/skeleton"}, "url"),
        ({"url": "https://10.0.0.1/skeleton"}, "url"),
        ({"url": "https://internal.example/skeleton"}, "url"),
        ({"url": "https://169.254.169.254/latest"}, "url"),
        ({"event_types": ["notes.internal_only"]}, "event_types"),
        ({"event_types": ["notes.unknown"]}, "event_types"),
        ({"event_types": []}, "event_types"),
    ],
)
def test_internal_urls_and_unexposed_events_are_refused(webhooks, overrides, field):
    fixture, principal, _ = webhooks
    response = api(login(fixture.admin), "post", collection(fixture.a), payload(principal, **overrides))
    assert response.status_code == 400 and field in response.json()["fields"]


def test_the_principal_is_an_active_service_account_of_the_same_organization(webhooks):
    fixture, principal, foreign = webhooks
    client = login(fixture.admin)
    for other in (foreign, fixture.alice):
        response = api(client, "post", collection(fixture.a), {**payload(principal), "principal": str(other.pk)})
        assert response.status_code == 400 and "principal" in response.json()["fields"]
    assert api(client, "post", collection(fixture.a), payload(principal, secret="mine")).status_code == 400


def test_without_the_capability_the_webhooks_do_not_exist(webhooks):
    from skeleton.access.models import MemberRole, Role

    fixture, principal, _ = webhooks
    created = api(login(fixture.admin), "post", collection(fixture.a), payload(principal)).json()
    with actor_tenant(fixture.admin, fixture.a):
        MemberRole(
            organization=fixture.a, membership=fixture.alice_membership, role=Role.objects.get(code="people_manager")
        ).save()
    for user in (fixture.alice, fixture.bob):  # A people manager of A; the administrator of B is a member of A.
        client = login(user)
        assert client.get(collection(fixture.a)).status_code == 404
        assert client.get(detail(fixture.a, created["sysid"])).status_code == 404
        assert client.get(reverse("integrations:webhooks", kwargs={"organization_id": fixture.a.pk})).status_code == 404
    # B's own administrator does not see A's endpoint in B either.
    bob = login(fixture.bob)
    assert bob.get(detail(fixture.b, created["sysid"])).status_code == 404
    assert bob.get(collection(fixture.b)).json()["count"] == 0


def test_rotation_issues_a_new_secret_and_keeps_the_old_one_for_the_overlap(webhooks):
    from skeleton.integrations.models import WebhookEndpoint
    from skeleton.integrations.webhooks.services import signing_secrets

    fixture, principal, _ = webhooks
    client = login(fixture.admin)
    created = api(client, "post", collection(fixture.a), payload(principal)).json()
    url = detail(fixture.a, created["sysid"], "integrations:api-webhook-rotate")
    assert api(client, "post", url).status_code == 428
    assert api(client, "post", url, revision=created["revision"] + 1).status_code == 412
    rotated = api(client, "post", url, revision=created["revision"])
    assert rotated.status_code == 200 and rotated.json()["secret"] != created["secret"]
    with actor_tenant(fixture.admin, fixture.a):
        endpoint = WebhookEndpoint.objects.get(pk=created["sysid"])
        assert signing_secrets(endpoint) == [rotated.json()["secret"], created["secret"]]
        later = timezone.now() + timedelta(hours=25)
        assert signing_secrets(endpoint, now=later) == [rotated.json()["secret"]]


def test_change_and_removal_use_compare_and_swap(webhooks):
    fixture, principal, _ = webhooks
    client = login(fixture.admin)
    created = api(client, "post", collection(fixture.a), payload(principal)).json()
    url = detail(fixture.a, created["sysid"])
    assert api(client, "patch", url, {"name": "Renamed"}).status_code == 428
    changed = api(client, "patch", url, {"name": "Renamed", "is_enabled": False}, revision=created["revision"])
    assert changed.status_code == 200 and changed.json()["name"] == "Renamed" and not changed.json()["is_enabled"]
    assert api(client, "patch", url, {"name": "Stale"}, revision=created["revision"]).status_code == 412
    assert (
        api(client, "patch", url, {"url": "https://10.1.1.1/x"}, revision=changed.json()["revision"]).status_code == 400
    )
    assert api(client, "delete", url, revision=changed.json()["revision"]).status_code == 204
    assert client.get(url).status_code == 404


def test_html_pages_issue_the_secret_once_and_require_recent_authentication(webhooks):
    fixture, principal, _ = webhooks
    client = login(fixture.admin)
    new = reverse("integrations:webhook-create", kwargs={"organization_id": fixture.a.pk})
    form = client.get(new)
    assert form.status_code == 200 and "notes.internal_only" not in form.content.decode()
    values = {
        "name": "HTML",
        "url": "https://hooks.example/html",
        "principal": str(principal.pk),
        "event_types": ["notes.webhook_ready"],
        "is_enabled": "on",
        "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
    }
    assert client.post(new, {k: v for k, v in values.items() if k != "csrfmiddlewaretoken"}).status_code == 403
    created = client.post(new, values)
    assert created.status_code == 201 and b"whsec_" in created.content and "no-store" in created["Cache-Control"]
    refused = client.post(new, {**values, "url": "https://internal.example/x"})
    assert refused.status_code == 400
    session = client.session
    session["account_authentication_methods"][-1]["at"] = 0
    session.save()
    stale = client.post(new, values)
    assert stale.status_code == 302 and stale["Location"].startswith(reverse("account_reauthenticate"))
    api_create = api(client, "post", collection(fixture.a), payload(principal))
    assert api_create.status_code == 403 and api_create.json()["code"] == "reauthentication_required"
