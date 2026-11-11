"""Token pages and REST (#47): shown once, own tokens only, isolation, tokens never issue tokens."""

import json

import pytest
from django.test import Client
from django.urls import reverse

from skeleton.accounts.services import create_service_account
from skeleton.integrations.tokens.models import ApiToken
from tools.security_fixtures import actor_tenant, build_security_fixture

PASSWORD = "Fixture-only-password-2026!"
NOTE_READ = "example_notes.note:read"


@pytest.fixture
def access(skeleton_db):
    return build_security_fixture()


def login(user):
    client = Client(enforce_csrf_checks=True)
    client.get(reverse("account_login"))
    response = client.post(
        reverse("account_login"),
        {"login": user.email, "password": PASSWORD, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
    )
    assert response.status_code == 302
    return client


def csrf(client):
    return client.cookies["csrftoken"].value


def create_html(client, url, **data):
    return client.post(
        url, {"name": "CLI", "scopes": [NOTE_READ], "days": 30, "csrfmiddlewaretoken": csrf(client), **data}
    )


def api(client, method, url, data=None, **headers):
    return getattr(client, method)(
        url, data=json.dumps(data or {}), content_type="application/json", HTTP_X_CSRFTOKEN=csrf(client), **headers
    )


def test_personal_token_is_shown_once_and_only_to_its_owner(access):
    alice = login(access.alice)
    url = reverse("integrations:personal-tokens")
    assert alice.get(url).status_code == 200
    created = create_html(alice, url)
    assert created.status_code == 201
    assert "no-store" in created["Cache-Control"]
    token = ApiToken.all_objects.get(user=access.alice)
    page = created.content.decode()
    assert f"skt_{token.prefix}_" in page
    text = page.split("data-token>")[1].split("<")[0]
    listing = alice.get(url).content.decode()
    assert f"skt_{token.prefix}_" in listing and text not in listing
    assert create_html(alice, url, days=10_000).status_code == 400
    assert create_html(alice, url, scopes=["sk_tenancy.membership:read"]).status_code == 400
    revoke = reverse("integrations:personal-token-revoke", args=[token.pk])
    bob = login(access.bob)
    assert bob.post(revoke, {"revision": token.revision, "csrfmiddlewaretoken": csrf(bob)}).status_code == 404
    assert access.alice.email not in bob.get(url).content.decode()
    assert alice.post(revoke, {"revision": token.revision, "csrfmiddlewaretoken": csrf(alice)}).status_code == 302
    assert ApiToken.all_objects.get(pk=token.pk).revoked_at is not None


def test_rest_issues_lists_and_revokes_with_a_session_only(access):
    alice = login(access.alice)
    url = reverse("integrations:api-me-tokens")
    created = api(alice, "post", url, {"name": "CI", "scopes": [NOTE_READ], "expires_at": "2099-01-01T00:00:00Z"})
    assert created.status_code == 400  # Above the maximum validity.
    from datetime import timedelta

    from django.utils import timezone

    expires = (timezone.now() + timedelta(days=5)).isoformat()
    created = api(alice, "post", url, {"name": "CI", "scopes": [NOTE_READ], "expires_at": expires})
    assert created.status_code == 201 and "no-store" in created["Cache-Control"]
    body = created.json()
    text, sysid = body["token"], body["sysid"]
    listing = alice.get(url).json()["results"]
    assert [row["sysid"] for row in listing] == [sysid] and "token" not in listing[0] and "digest" not in listing[0]
    by_token = Client(enforce_csrf_checks=True, HTTP_AUTHORIZATION=f"Bearer {text}")
    for response in (by_token.get(url), by_token.post(url, data="{}", content_type="application/json")):
        assert response.status_code == 403 and response.json()["code"] == "token_not_allowed"
    detail = reverse("integrations:api-me-token", args=[sysid])
    assert by_token.delete(detail).status_code == 403
    assert api(alice, "delete", detail).status_code == 428  # If-Match required.
    bob = login(access.bob)
    assert api(bob, "delete", detail, HTTP_IF_MATCH='"1"').status_code == 404
    assert api(alice, "delete", detail, HTTP_IF_MATCH='"1"').status_code == 204


def test_service_account_tokens_are_isolated_by_organization(access):
    with actor_tenant(access.admin, access.a):
        account = create_service_account(actor=access.admin, name="ERP")
    admin = login(access.admin)
    accounts = reverse("integrations:service-accounts", kwargs={"organization_id": access.a.pk})
    assert "ERP" in admin.get(accounts).content.decode()
    detail = reverse("integrations:service-account", kwargs={"organization_id": access.a.pk, "sysid": account.pk})
    assert create_html(admin, detail).status_code == 201
    token = ApiToken.all_objects.get(user=account)
    assert token.bound_organization_id == access.a.pk
    # Bob is a plain member of A (no integrations.manage) and administers B.
    bob = login(access.bob)
    assert bob.get(accounts).status_code == 404
    assert bob.get(detail).status_code == 404
    foreign = reverse("integrations:service-account", kwargs={"organization_id": access.b.pk, "sysid": account.pk})
    assert bob.get(foreign).status_code == 404
    revoke = reverse(
        "integrations:service-token-revoke",
        kwargs={"organization_id": access.b.pk, "sysid": account.pk, "token_id": token.pk},
    )
    assert bob.post(revoke, {"revision": token.revision, "csrfmiddlewaretoken": csrf(bob)}).status_code == 404
    rest = reverse(
        "integrations:api-service-account-tokens", kwargs={"organization_id": access.b.pk, "sysid": account.pk}
    )
    assert bob.get(rest).status_code == 404
    assert ApiToken.all_objects.get(pk=token.pk).revoked_at is None
    own = reverse(
        "integrations:api-service-account-tokens", kwargs={"organization_id": access.a.pk, "sysid": account.pk}
    )
    assert [row["prefix"] for row in admin.get(own).json()["results"]] == [token.prefix]
    deactivate = reverse(
        "integrations:service-account-deactivate", kwargs={"organization_id": access.a.pk, "sysid": account.pk}
    )
    assert admin.post(deactivate, {"csrfmiddlewaretoken": csrf(admin)}).status_code == 302
    from skeleton.integrations.tokens.services import verify_token

    assert verify_token(f"skt_{token.prefix}_" + "A" * 43) is None
