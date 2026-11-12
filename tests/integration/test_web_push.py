"""Web push subscriptions and delivery through a fake push service (#56).

Runs on PostgreSQL with the runtime role.
"""

import json

import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.test import Client, override_settings
from django.urls import reverse

from skeleton.common import outbound_http
from skeleton.notifications.channels.push import b64url
from skeleton.notifications.models import NotificationDelivery, WebPushSubscription
from skeleton.notifications.registry import NotificationType, register_notification_type
from skeleton.notifications.services import Recipients, notify
from skeleton.tenancy.context import system_context
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.registries import isolated_notification_types
from skeleton.testing.service_api import FIXTURE_PASSWORD, build_service_scenario
from tests import support

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]
ENDPOINT = "https://fcm.googleapis.com/fcm/send/browser-one"
BROWSER = ec.generate_private_key(ec.SECP256R1())
BROWSER_PUBLIC = BROWSER.public_key().public_bytes(
    serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
)
AUTH = b"0123456789abcdef"
KEYS = {"p256dh": b64url(BROWSER_PUBLIC), "auth": b64url(AUTH)}


def browser_decrypt(body):
    salt, length = body[:16], body[20]
    sender_bytes = body[21 : 21 + length]
    sender = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), sender_bytes)
    shared = BROWSER.exchange(ec.ECDH(), sender)
    ikm = HKDF(hashes.SHA256(), 32, AUTH, b"WebPush: info\x00" + BROWSER_PUBLIC + sender_bytes).derive(shared)
    key = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(ikm)
    return json.loads(AESGCM(key).decrypt(nonce, body[21 + length :], None)[:-1])


class FakePushService:
    def __init__(self, monkeypatch):
        self.calls, self.status = [], 201
        monkeypatch.setattr(outbound_http, "request", self.request)

    def request(self, method, url, *, headers=None, body=None, resolver=None):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        return outbound_http.OutboundResponse(status=self.status, headers={}, body=b"", truncated=False)


@pytest.fixture
def scenario(monkeypatch, tmp_path):

    from examples.notes.models import Note

    key = ec.generate_private_key(ec.SECP256R1())
    path = tmp_path / "vapid.pem"
    path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    with isolated_notification_types():
        register_notification_type(
            NotificationType(
                "notes.mention",
                "Você foi mencionado",
                snapshot_fields=("title",),
                target_table_key="example_notes.note",
                default_channels=("in_app", "push"),
            )
        )
        register_notification_type(
            NotificationType(
                "notes.shared",
                "Nota compartilhada",
                snapshot_fields=("title",),
                target_table_key="example_notes.note",
                default_channels=("in_app", "push"),
                push_snapshot=True,
            )
        )
        with override_settings(
            WEBPUSH_VAPID_PRIVATE_KEY_FILE=str(path),
            WEBPUSH_VAPID_SUBJECT="mailto:ops@example.test",
            SKELETON_PUBLIC_URL="https://app.example.test",
            INTEGRATION_ENCRYPTION_KEYS=[Fernet.generate_key().decode()],
        ):
            yield build_service_scenario(Note, slug="push")


def login(user):
    return support.login(user, password=FIXTURE_PASSWORD)


def subscribe(client, endpoint=ENDPOINT, keys=KEYS, csrf=True):
    headers = {"X-CSRFToken": client.cookies["csrftoken"].value} if csrf else {}
    return client.post(
        reverse("notifications:api-push-subscribe"),
        json.dumps({"endpoint": endpoint, "keys": keys}),
        content_type="application/json",
        headers=headers,
    )


def mention(scenario, note, code="notes.mention"):
    with scenario.acting(scenario.admin_a, scenario.a):
        notify(code, recipients=Recipients(users=(scenario.member.pk,)), target=note)


def subscriptions(user, *, deleted=False):
    with system_context(reason="test:push-rows"):
        rows = WebPushSubscription.all_objects.filter(user=user)
        return list(rows.filter(deleted_at__isnull=not deleted))


def test_subscription_requires_session_csrf_and_an_allowlisted_endpoint(scenario):
    client = login(scenario.member)
    assert subscribe(client, csrf=False).status_code == 403
    refused = subscribe(client, endpoint="https://169.254.169.254/latest/meta-data/")
    assert refused.status_code == 400
    assert subscribe(client, endpoint="https://push.example.test/x").status_code == 400
    assert subscribe(client, keys={"p256dh": "nope", "auth": "x"}).status_code == 400
    assert subscribe(Client(), csrf=False).status_code == 403
    created = subscribe(client)
    assert created.status_code == 201
    [row] = subscriptions(scenario.member)
    assert row.endpoint.reveal() == ENDPOINT and "fcm.googleapis.com" not in repr(row.endpoint)
    with system_context(reason="test:push-raw"):
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute(f"SELECT endpoint FROM {WebPushSubscription._meta.db_table}")
            assert "fcm.googleapis.com" not in cursor.fetchone()[0]
    other = login(scenario.denied)
    assert subscribe(other).status_code == 201  # Same browser, another person: the endpoint moves.
    assert subscriptions(scenario.member) == [] and len(subscriptions(scenario.denied)) == 1


def test_push_payload_is_minimal_encrypted_and_signed(scenario, monkeypatch):
    service = FakePushService(monkeypatch)
    subscribe(login(scenario.member))
    note = scenario.create(scenario.a, owner=scenario.admin_a, code="P-1", title="Título secreto")
    mention(scenario, note)
    run_pending_jobs()
    [call] = service.calls
    assert call["url"] == ENDPOINT and call["headers"]["Content-Encoding"] == "aes128gcm"
    assert call["headers"]["Authorization"].startswith("vapid t=") and call["headers"]["TTL"]
    payload = browser_decrypt(call["body"])
    assert payload["title"] == "Você foi mencionado" and "body" not in payload
    assert payload["url"].startswith("https://app.example.test/o/")
    assert b"secreto" not in call["body"]
    mention(scenario, note, code="notes.shared")
    run_pending_jobs()
    assert browser_decrypt(service.calls[1]["body"])["body"] == "Título secreto"
    with system_context(reason="test:push-deliveries", organization_id=scenario.a.pk):
        states = {row.state for row in NotificationDelivery.objects.filter(channel="push")}
    assert states == {"sent"}


def test_gone_subscription_is_removed_logically(scenario, monkeypatch):
    service = FakePushService(monkeypatch)
    service.status = 410
    subscribe(login(scenario.member))
    note = scenario.create(scenario.a, owner=scenario.admin_a, code="P-2", title="Expirada")
    mention(scenario, note)
    run_pending_jobs()
    assert len(service.calls) == 1
    assert subscriptions(scenario.member) == [] and len(subscriptions(scenario.member, deleted=True)) == 1
    mention(scenario, note)
    run_pending_jobs()
    assert len(service.calls) == 1  # Nothing is sent to a removed subscription.


def test_server_errors_are_retried(scenario, monkeypatch):
    from datetime import timedelta

    from skeleton.notifications.channels import delivery

    monkeypatch.setattr(delivery, "RETRY_SCHEDULE", (timedelta(0),) * 4)
    service = FakePushService(monkeypatch)
    service.status = 503
    subscribe(login(scenario.member))
    note = scenario.create(scenario.a, owner=scenario.admin_a, code="P-3", title="Instável")
    mention(scenario, note)
    run_pending_jobs()
    assert len(service.calls) == delivery.MAX_ATTEMPTS
    with system_context(reason="test:push-deliveries", organization_id=scenario.a.pk):
        [dead] = NotificationDelivery.objects.filter(channel="push")
    assert (dead.state, dead.error_code) == ("dead", "http_503")


def test_page_keeps_push_hidden_without_javascript(scenario):
    client = login(scenario.member)
    html = client.get(reverse("notifications:preferences")).content.decode()
    assert "data-push-optin" in html and 'data-push-key="' in html
    section = html[html.index("data-push-optin") :]
    assert section.split(">", 1)[0].rstrip().endswith("hidden")
    worker = client.get(reverse("notifications:service-worker"))
    assert worker.status_code == 200 and worker["Service-Worker-Allowed"] == "/"
    with override_settings(WEBPUSH_VAPID_PRIVATE_KEY_FILE=""):
        assert "data-push-optin" not in client.get(reverse("notifications:preferences")).content.decode()
        assert client.get(reverse("notifications:service-worker")).status_code == 404
