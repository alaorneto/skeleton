"""OIDC access tokens of native clients on /api/v1/ (#604): the same barriers as the skt_ tokens."""

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from skeleton.tenancy.context import request_context, system_context
from tests import oidc_support as oidc
from tests import support
from tools.security_fixtures import build_security_fixture

pytestmark = pytest.mark.needs_example_app

NOTE = "example_notes.note"


@pytest.fixture
def access(skeleton_db, settings):
    oidc.enable_provider(settings)
    fixture = build_security_fixture()
    oidc.register()
    return fixture


def tokens_of(user, scope="openid profile email"):
    return oidc.sign_in(support.login(user, password=oidc.PASSWORD, csrf=False), scope=scope)


def bearer(text):
    return Client(enforce_csrf_checks=True, HTTP_AUTHORIZATION=f"Bearer {text}")


def records_url(organization):
    return reverse("api:records", kwargs={"organization_id": organization.pk, "table_key": NOTE})


def create_note(client, organization, code):
    return client.post(
        records_url(organization),
        data=json.dumps({"code": code, "title": code, "body": "body"}),
        content_type="application/json",
    )


def assert_invalid(response):
    assert response.status_code == 401, response.content
    assert response.json()["code"] == "invalid_token"
    assert 'error="invalid_token"' in response["WWW-Authenticate"]


def test_scope_mapping_is_broad_for_oidc_scopes_and_narrow_for_core_scopes(skeleton_db):
    from skeleton.integrations.tokens.oidc import broad_scopes, core_scopes

    broad = core_scopes(["openid", "profile", "email"])
    assert broad == broad_scopes() and f"{NOTE}:create" in broad and "files:read" in broad
    assert not any(scope.endswith(".feed") for scope in broad)
    assert core_scopes(["openid", f"{NOTE}:read"]) == (f"{NOTE}:read",)


def test_token_acts_as_the_person_without_widening_the_role(access):
    client = bearer(tokens_of(access.alice)["access_token"])
    me = client.get(reverse("api:me"))
    assert me.status_code == 200 and me.json()["email"] == access.alice.email
    assert client.get(reverse("api:organizations")).status_code == 200
    created = create_note(client, access.a, "by-oidc")
    assert created.status_code == 201, created.content
    assert create_note(client, access.b, "denied").status_code == 403  # No table permission in B.
    outsider = bearer(tokens_of(access.outsider)["access_token"])
    assert outsider.get(records_url(access.a)).status_code == 404
    # Tokens never administer: the administrator's token cannot reach access administration.
    admin = bearer(tokens_of(access.admin)["access_token"])
    memberships = reverse("api:access-list", kwargs={"organization_id": access.a.pk, "resource": "memberships"})
    assert admin.get(memberships).status_code == 403
    patched = admin.patch(reverse("api:me"), data="{}", content_type="application/json")
    assert patched.status_code == 403 and patched.json()["code"] == "token_not_allowed"
    from skeleton.audit.models import AuditEvent

    with request_context(actor=access.alice, request_id=uuid4(), source="test:audit"):
        from skeleton.tenancy.context import tenant_context

        with tenant_context(organization_id=access.a.pk, actor=access.alice, source="test:audit"):
            sources = set(
                AuditEvent.objects.filter(record_sysid=created.json()["sysid"]).values_list("source", flat=True)
            )
    assert sources == {f"http:oidc:{oidc.CLIENT_ID}"}


def test_registered_core_scopes_narrow_the_token(skeleton_db, settings):
    oidc.enable_provider(settings)
    access = build_security_fixture()
    oidc.register(scopes=[f"{NOTE}:read"])
    client = bearer(tokens_of(access.alice, scope=f"openid {NOTE}:read")["access_token"])
    assert client.get(records_url(access.a)).status_code == 200
    assert create_note(client, access.a, "denied").status_code == 403


def test_invalid_tokens_answer_401(access, subtests):
    from allauth.idp.oidc.models import Token

    from skeleton.accounts.idp.clients import revoke_all
    from skeleton.accounts.services import revoke_sessions

    with subtests.test("unknown"):
        assert_invalid(bearer("not-a-token").get(reverse("api:me")))
    with subtests.test("expired"):
        tokens = tokens_of(access.alice)
        Token.objects.filter(type="at").update(expires_at=timezone.now() - timedelta(seconds=1))
        assert_invalid(bearer(tokens["access_token"]).get(reverse("api:me")))
    with subtests.test("revoked"):
        tokens = tokens_of(access.alice)
        revoked = Client().post(reverse("oidc-revoke"), {"token": tokens["access_token"], "client_id": oidc.CLIENT_ID})
        assert revoked.status_code == 200
        assert_invalid(bearer(tokens["access_token"]).get(reverse("api:me")))
    with subtests.test("auth_version"):
        tokens = tokens_of(access.alice)
        with system_context(reason="test:revoke-sessions"):
            revoke_sessions(access.alice)
        assert_invalid(bearer(tokens["access_token"]).get(reverse("api:me")))
    with subtests.test("client revoked"):
        tokens = tokens_of(access.bob)
        with system_context(reason="test:disable"):
            from skeleton.accounts.models import NativeClient

            NativeClient.objects.filter(client_id=oidc.CLIENT_ID).update(enabled=False)
        assert_invalid(bearer(tokens["access_token"]).get(reverse("api:me")))
        with system_context(reason="test:revoke-all"):
            revoke_all(client_id=oidc.CLIENT_ID)
        assert_invalid(bearer(tokens["access_token"]).get(reverse("api:me")))


def test_ambiguity_rate_limit_switch_and_openapi(access, settings, subtests):
    tokens = tokens_of(access.alice)
    with subtests.test("session and bearer"):
        session = support.login(access.alice, password=oidc.PASSWORD)
        response = session.get(reverse("api:me"), HTTP_AUTHORIZATION=f"Bearer {tokens['access_token']}")
        assert response.status_code == 400 and response.json()["code"] == "ambiguous_credentials"
    with subtests.test("openapi"):
        schema = bearer(tokens["access_token"]).get(reverse("api:openapi")).json()
        scheme = schema["components"]["securitySchemes"]["openIdConnect"]
        assert scheme["openIdConnectUrl"] == "http://testserver/.well-known/openid-configuration"
        me = schema["paths"]["/api/v1/me/"]["get"]["security"]
        assert {"openIdConnect": []} in me and {"bearerAuth": []} in me
    with subtests.test("rate limit per token"):
        settings.SKELETON_API_TOKEN_RATE_PER_MINUTE = 2
        client = bearer(oidc.refresh(tokens["refresh_token"]).json()["access_token"])
        statuses = [client.get(reverse("api:me")).status_code for _ in range(3)]
        assert statuses == [200, 200, 429]
        settings.SKELETON_API_TOKEN_RATE_PER_MINUTE = 600
    with subtests.test("rollback switch"):
        settings.SKELETON_OIDC_API_TOKENS = False
        fresh = tokens_of(access.bob)
        assert_invalid(bearer(fresh["access_token"]).get(reverse("api:me")))


def test_personal_tokens_still_work(access):
    from skeleton.integrations.tokens.services import issue_personal_token

    with request_context(actor=access.alice, request_id=uuid4(), source="test:tokens"):
        issued = issue_personal_token(
            actor=access.alice, name="CLI", scopes=[f"{NOTE}:read"], expires_at=timezone.now() + timedelta(days=7)
        )
    assert bearer(issued.text).get(records_url(access.a)).status_code == 200
