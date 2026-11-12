"""Bearer tokens on /api/v1/ (#46): CSRF, ambiguity, scopes, binding, invalid tokens and audit."""

import json
import logging
from datetime import timedelta
from uuid import uuid4

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from skeleton.accounts.services import create_service_account
from skeleton.integrations.tokens.scopes import register_token_scope
from skeleton.integrations.tokens.services import issue_personal_token, issue_service_token, revoke_token
from skeleton.tenancy.context import request_context, system_context
from skeleton.testing.registries import isolated_token_scopes, table_spec_override
from tests import support
from tools.security_fixtures import actor_tenant, build_security_fixture

pytestmark = pytest.mark.needs_example_app

PASSWORD = "Fixture-only-password-2026!"
NOTE = "example_notes.note"


@pytest.fixture
def access(skeleton_db):
    return build_security_fixture()


def token_for(user, scopes):
    with request_context(actor=user, request_id=uuid4(), source="test:tokens"):
        return issue_personal_token(
            actor=user, name="CLI", scopes=list(scopes), expires_at=timezone.now() + timedelta(days=7)
        )


def bearer(text):
    return Client(enforce_csrf_checks=True, HTTP_AUTHORIZATION=f"Bearer {text}")


def records_url(organization):
    return reverse("api:records", kwargs={"organization_id": organization.pk, "table_key": NOTE})


def login(user):
    return support.login(user, password=PASSWORD)


def create_note(client, organization, code):
    return client.post(
        records_url(organization),
        data=json.dumps({"code": code, "title": code, "body": "body"}),
        content_type="application/json",
    )


def test_token_needs_no_csrf_and_the_session_still_does(access):
    issued = token_for(access.alice, [f"{NOTE}:read", f"{NOTE}:create"])
    client = bearer(issued.text)
    listing = client.get(records_url(access.a))
    assert listing.status_code == 200
    assert {row["code"] for row in listing.json()["results"]} >= {"private", "shared"}
    created = create_note(client, access.a, "by-token")
    assert created.status_code == 201, created.content
    session = login(access.alice)
    assert create_note(session, access.a, "no-csrf").status_code == 403


def test_session_and_bearer_together_are_ambiguous(access):
    issued = token_for(access.alice, [f"{NOTE}:read"])
    session = login(access.alice)
    response = session.get(records_url(access.a), HTTP_AUTHORIZATION=f"Bearer {issued.text}")
    assert response.status_code == 400 and response.json()["code"] == "ambiguous_credentials"


def test_scope_narrows_the_role_and_never_widens_it(access):
    read_only = bearer(token_for(access.alice, [f"{NOTE}:read"]).text)
    assert read_only.get(records_url(access.a)).status_code == 200
    assert create_note(read_only, access.a, "denied").status_code == 403  # Role allows, scope does not.
    create_only = bearer(token_for(access.alice, [f"{NOTE}:create"]).text)
    assert create_only.get(records_url(access.a)).status_code == 403
    # In B, Alice's role has no table permission: the scope cannot add it.
    wide = bearer(token_for(access.alice, [f"{NOTE}:read", f"{NOTE}:create"]).text)
    assert wide.get(records_url(access.b)).status_code == 403
    outsider = bearer(token_for(access.outsider, [f"{NOTE}:read"]).text)
    assert outsider.get(records_url(access.a)).status_code == 404  # Not a member: like a missing tenant.


def test_tokens_cannot_reach_administration_and_patch_me(access, monkeypatch):

    from skeleton.system.registry import AttachmentPolicy

    policy = AttachmentPolicy(max_bytes=200, types=("application/pdf",))
    with table_spec_override(NOTE, attachments=policy):
        client = bearer(token_for(access.admin, [f"{NOTE}:read"]).text)
        assert client.get(reverse("api:me")).status_code == 200
        patched = client.patch(reverse("api:me"), data="{}", content_type="application/json")
        assert patched.status_code == 403 and patched.json()["code"] == "token_not_allowed"
        memberships = reverse("api:access-list", kwargs={"organization_id": access.a.pk, "resource": "memberships"})
        assert client.get(memberships).status_code == 403
        audit = reverse("api:audit", kwargs={"organization_id": access.a.pk})
        assert client.get(audit).status_code == 403
        attachments = reverse(
            "api:attachments", kwargs={"organization_id": access.a.pk, "table_key": NOTE, "sysid": access.shared.pk}
        )
        assert client.get(attachments).status_code == 403  # files:read missing.
        files = bearer(token_for(access.admin, [f"{NOTE}:read", "files:read"]).text)
        assert files.get(attachments).status_code == 200


def test_bound_token_on_another_organization_is_not_found(access):
    with actor_tenant(access.admin, access.a):
        account = create_service_account(actor=access.admin, name="ERP")
    with system_context(reason="test:service-grant", organization_id=access.a.pk):
        from skeleton.access.models import MemberRole, Role
        from skeleton.tenancy.models import Membership

        membership = Membership.objects.get(user=account)
        MemberRole(organization=access.a, membership=membership, role=Role.objects.get(code="member")).save()
    with actor_tenant(access.admin, access.a):
        issued = issue_service_token(
            actor=access.admin,
            account=account,
            name="ERP",
            scopes=[f"{NOTE}:read"],
            expires_at=timezone.now() + timedelta(days=7),
        )
    client = bearer(issued.text)
    assert client.get(records_url(access.a)).status_code == 200
    assert client.get(records_url(access.b)).status_code == 404


def test_invalid_tokens_answer_401_with_a_challenge(access, caplog):
    from skeleton.accounts.models import User

    revoked = token_for(access.alice, [f"{NOTE}:read"])
    with request_context(actor=access.alice, request_id=uuid4(), source="test:tokens"):
        revoke_token(actor=access.alice, token_id=revoked.token.pk, expected_revision=revoked.token.revision)
    deactivated = token_for(access.bob, [f"{NOTE}:read"])
    with system_context(reason="test:token-auth-version"):
        bob = User.objects.get(pk=access.bob.pk)
        bob.auth_version += 1
        bob.save(update_fields=["auth_version"])
    with caplog.at_level(logging.INFO, logger="skeleton.security"):
        for text in (revoked.text, deactivated.text, "skt_00000000_" + "A" * 43, "garbage"):
            response = bearer(text).get(records_url(access.a))
            assert response.status_code == 401
            assert response.json()["code"] == "invalid_token"
            assert response["WWW-Authenticate"].startswith("Bearer")
    assert [getattr(r, "reason_code", None) for r in caplog.records if getattr(r, "event", "") == "api_token.rejected"]
    assert revoked.text not in caplog.text


def test_feed_tokens_are_not_api_credentials(access):
    with isolated_token_scopes():
        register_token_scope("test.feed", "Feed de teste", feed=True)

        with request_context(actor=access.alice, request_id=uuid4(), source="test:tokens"):
            feed = issue_personal_token(
                actor=access.alice,
                name="Feed",
                scopes=["test.feed"],
                expires_at=timezone.now() + timedelta(days=7),
                feed=True,
            )
        response = bearer(feed.text).get(reverse("api:me"))
        assert response.status_code == 401 and response.json()["code"] == "invalid_token"


def test_html_routes_ignore_the_bearer_header(access):
    client = bearer(token_for(access.alice, [f"{NOTE}:read"]).text)
    response = client.get(reverse("web:dashboard", kwargs={"organization_id": access.a.pk}))
    assert response.status_code == 302 and "/accounts/login/" in response["Location"]


def test_audit_records_the_token_user_and_prefix(access):
    from skeleton.audit.models import AuditEvent

    issued = token_for(access.alice, [f"{NOTE}:create"])
    created = create_note(bearer(issued.text), access.a, "audited")
    assert created.status_code == 201
    with actor_tenant(access.admin, access.a):
        event = AuditEvent.objects.get(record_sysid=created.json()["sysid"], operation="create")
    assert event.actor_sysid == access.alice.pk
    assert event.source == f"http:token:{issued.token.prefix}"
