"""Token pages and REST (#47): shown once, own tokens only, isolation, tokens never issue tokens."""

import json

import pytest
from django.test import Client
from django.urls import reverse

from skeleton.accounts.services import create_service_account
from skeleton.integrations.tokens.models import ApiToken
from tests import support
from tools.security_fixtures import actor_tenant, build_security_fixture

pytestmark = pytest.mark.needs_example_app

PASSWORD = "Fixture-only-password-2026!"
NOTE_READ = "example_notes.note:read"


@pytest.fixture
def access(skeleton_db):
    return build_security_fixture()


def login(user):
    return support.login(user, password=PASSWORD)


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


def stale(client):
    session = client.session
    session["account_authentication_methods"][-1]["at"] = 0
    session.save()
    return client


def soon_iso(days=5):
    from datetime import timedelta

    from django.utils import timezone

    return (timezone.now() + timedelta(days=days)).isoformat()


def if_match(body):
    return {"HTTP_IF_MATCH": f'"{body["revision"]}"'}


def test_api_revocation_and_service_account_changes_require_recent_authentication(access):
    """Revoking and creating or deactivating service accounts need a fresh login, as issuing does (#327)."""
    from skeleton.accounts.models import User

    mine = reverse("integrations:api-me-tokens")
    personal = api(login(access.alice), "post", mine, {"name": "CI", "scopes": [NOTE_READ], "expires_at": soon_iso()})
    personal = personal.json()
    accounts = reverse("integrations:api-service-accounts", kwargs={"organization_id": access.a.pk})
    admin = login(access.admin)
    account = api(admin, "post", accounts, {"name": "ERP"}).json()
    kwargs = {"organization_id": access.a.pk, "sysid": account["sysid"]}
    tokens = reverse("integrations:api-service-account-tokens", kwargs=kwargs)
    service = api(admin, "post", tokens, {"name": "ERP", "scopes": [NOTE_READ], "expires_at": soon_iso()}).json()
    account_url = reverse("integrations:api-service-account", kwargs=kwargs)
    service_url = reverse("integrations:api-service-account-token", kwargs={**kwargs, "token_id": service["sysid"]})
    personal_url = reverse("integrations:api-me-token", args=[personal["sysid"]])

    def refused(response):
        return response.status_code == 403 and response.json()["code"] == "reauthentication_required"

    old_alice, old_admin = stale(login(access.alice)), stale(login(access.admin))
    assert refused(api(old_alice, "delete", personal_url, **if_match(personal)))
    assert refused(api(old_admin, "post", accounts, {"name": "Other"}))
    assert refused(api(old_admin, "delete", account_url))
    assert refused(api(old_admin, "delete", service_url, **if_match(service)))
    # Without integrations.manage the routes do not exist, before any other answer.
    for bob in (login(access.bob), stale(login(access.bob))):
        assert api(bob, "post", accounts, {"name": "Bob"}).status_code == 404
        assert api(bob, "delete", account_url).status_code == 404
        assert api(bob, "delete", service_url, **if_match(service)).status_code == 404
    assert ApiToken.objects.filter(pk__in=[personal["sysid"], service["sysid"]]).count() == 2
    with actor_tenant(access.admin, access.a):
        assert User.objects.get(pk=account["sysid"]).is_active
        assert not User.objects.filter(first_name__in=["Other", "Bob"]).exists()
    fresh_alice, fresh_admin = login(access.alice), login(access.admin)
    assert api(fresh_alice, "delete", personal_url, **if_match(personal)).status_code == 204
    assert api(fresh_admin, "delete", service_url, **if_match(service)).status_code == 204
    assert api(fresh_admin, "delete", account_url).status_code == 204
    assert api(fresh_admin, "post", accounts, {"name": "Other"}).status_code == 201


def test_tokens_that_no_longer_authenticate_are_listed_as_unusable(access):
    """A deactivated account or a changed auth_version leaves the token in the list, marked unusable (#327)."""
    from skeleton.accounts.models import User
    from skeleton.accounts.services import deactivate_service_account
    from skeleton.tenancy.context import system_context

    mine = reverse("integrations:api-me-tokens")
    alice = login(access.alice)
    created = api(alice, "post", mine, {"name": "CI", "scopes": [NOTE_READ], "expires_at": soon_iso()})
    assert created.status_code == 201
    assert [row["usable"] for row in alice.get(mine).json()["results"]] == [True]
    page = reverse("integrations:personal-tokens")
    html = alice.get(page).content.decode()
    assert "Ativo" in html and "Inválido" not in html
    with system_context(reason="test:token-usable"):
        user = User.objects.get(pk=access.alice.pk)
        user.auth_version += 1
        user.save(update_fields=["auth_version"])
    alice = login(access.alice)  # The session follows auth_version too.
    assert [row["usable"] for row in alice.get(mine).json()["results"]] == [False]
    assert "Inválido" in alice.get(page).content.decode()
    with actor_tenant(access.admin, access.a):
        account = create_service_account(actor=access.admin, name="ERP")
    admin = login(access.admin)
    kwargs = {"organization_id": access.a.pk, "sysid": account.pk}
    tokens = reverse("integrations:api-service-account-tokens", kwargs=kwargs)
    body = {"name": "ERP", "scopes": [NOTE_READ], "expires_at": soon_iso()}
    assert api(admin, "post", tokens, body).status_code == 201
    assert [row["usable"] for row in admin.get(tokens).json()["results"]] == [True]
    with actor_tenant(access.admin, access.a):
        deactivate_service_account(actor=access.admin, user=account)
    assert [row["usable"] for row in admin.get(tokens).json()["results"]] == [False]
    detail = reverse("integrations:service-account", kwargs=kwargs)
    assert "Inválido" in admin.get(detail).content.decode()


def test_token_and_service_account_lists_are_paginated(access):
    """The three API lists answer count/page/page_size/results and refuse other parameters (#327)."""
    mine = reverse("integrations:api-me-tokens")
    alice = login(access.alice)
    for name in ("one", "two", "three"):
        body = {"name": name, "scopes": [NOTE_READ], "expires_at": soon_iso()}
        assert api(alice, "post", mine, body).status_code == 201
    first = alice.get(mine, {"page_size": 2}).json()
    second = alice.get(mine, {"page_size": 2, "page": 2}).json()
    assert (first["count"], first["page"], first["page_size"], len(first["results"])) == (3, 1, 2, 2)
    assert (second["page"], len(second["results"])) == (2, 1)
    assert len({row["sysid"] for row in first["results"] + second["results"]}) == 3
    admin = login(access.admin)
    accounts = reverse("integrations:api-service-accounts", kwargs={"organization_id": access.a.pk})
    for name in ("ERP", "CRM"):
        assert api(admin, "post", accounts, {"name": name}).status_code == 201
    listed = admin.get(accounts, {"page_size": 1}).json()
    assert (listed["count"], len(listed["results"])) == (2, 1)
    account = listed["results"][0]["sysid"]
    tokens = reverse(
        "integrations:api-service-account-tokens", kwargs={"organization_id": access.a.pk, "sysid": account}
    )
    body = {"name": "x", "scopes": [NOTE_READ], "expires_at": soon_iso()}
    assert api(admin, "post", tokens, body).status_code == 201
    paged = admin.get(tokens).json()
    assert {"count", "page", "page_size", "results"} <= set(paged) and paged["count"] == 1
    for client, url in ((alice, mine), (admin, accounts), (admin, tokens)):
        refused = client.get(url, {"name": "x"})
        assert refused.status_code == 400 and refused.json()["code"] == "invalid_filter", url
    html = admin.get(reverse("integrations:service-accounts", kwargs={"organization_id": access.a.pk}))
    assert html.status_code == 200 and "ERP" in html.content.decode()
