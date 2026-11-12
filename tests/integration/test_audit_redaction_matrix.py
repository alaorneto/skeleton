"""Sensitive fields of core tables leave the audit as [REDACTED] (#263, AGENTS.md checklist)."""

import json
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.db import connection
from django.test import RequestFactory, override_settings

from tools.security_fixtures import actor_tenant, build_security_fixture

pytestmark = pytest.mark.needs_example_app

REDACTED = {"new": "[REDACTED]", "redacted": True}


@pytest.fixture
def fixture(skeleton_db):
    assert connection.vendor == "postgresql"
    return build_security_fixture()


def _redacted(change):
    return {key: change[key] for key in REDACTED} == REDACTED


def test_invitation_token_digest_is_redacted(fixture):
    from skeleton.audit.models import AuditEvent
    from skeleton.tenancy.invitations import issue_invitation

    with actor_tenant(fixture.admin, fixture.a):
        invitation = issue_invitation(
            user=fixture.admin,
            email="redaction@example.test",
            role=fixture.member_role,
            request=RequestFactory().get("/"),
        )
        invitation = type(invitation).objects.get(pk=invitation.pk)
        changes = AuditEvent.objects.get(record_sysid=invitation.pk, operation="create").changes
    assert changes["token_digest"] == {"old": None, "new": "[REDACTED]", "redacted": True}
    assert invitation.token_digest and invitation.token_digest not in json.dumps(changes)
    assert changes["organization_id"]["redacted"] is False  # Positive control: a plain field is readable.


def test_app_configuration_is_redacted(fixture):
    from skeleton.audit.models import AuditEvent
    from skeleton.system.applications import application_state, configure_application

    marker = "Heading redaction marker 263"
    with actor_tenant(fixture.admin, fixture.a):
        state = application_state("example_notes")
        installation = configure_application(
            "example_notes",
            user=fixture.admin,
            enabled=True,
            configuration={"heading": marker},
            revision=state["revision"],
        )
        events = list(AuditEvent.objects.filter(record_sysid=installation.pk).order_by("created_at"))
    assert events
    for event in events:
        assert marker not in json.dumps(event.changes)
    configured = [event.changes["configuration"] for event in events if "configuration" in event.changes]
    assert configured and all(_redacted(change) for change in configured), configured
    # Positive control: plain fields of the same rows stay readable.
    assert any(not change["redacted"] for event in events for change in event.changes.values())


@override_settings(INTEGRATION_ENCRYPTION_KEYS=[Fernet.generate_key().decode()])
def test_push_subscription_secrets_are_redacted(fixture):
    from skeleton.accounts.services import bootstrap_identity
    from skeleton.audit.models import AuditEvent
    from skeleton.notifications.channels import push
    from skeleton.notifications.models import WebPushSubscription
    from skeleton.tenancy.context import request_context

    browser = ec.generate_private_key(ec.SECP256R1()).public_key()
    p256dh = push.b64url(
        browser.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    )
    auth = push.b64url(b"redaction-auth-1")
    endpoint = "https://fcm.googleapis.com/fcm/send/redaction-matrix-263"
    with request_context(actor=fixture.alice, request_id=uuid4(), source="test:redaction-push"):
        subscription = push.subscribe(fixture.alice, endpoint=endpoint, p256dh=p256dh, auth=auth)
        stored = WebPushSubscription.objects.get(pk=subscription.pk)
    platform, _ = bootstrap_identity(
        email="redaction-platform@example.test",
        organization="redaction-platform",
        name="Redaction",
        password="Kq9-unrelated-Phrase-2026!",
    )
    with request_context(actor=platform, request_id=uuid4(), source="test:redaction"):
        changes = AuditEvent.objects.get(record_sysid=stored.pk, operation="create").changes
    for name in ("endpoint", "endpoint_digest", "p256dh", "auth_secret"):
        assert _redacted(changes[name]), name
    text = json.dumps(changes)
    for secret in (endpoint, stored.endpoint_digest, p256dh, auth):
        assert secret not in text
    assert changes["user_id"]["redacted"] is False  # Positive control: a plain field is readable.
