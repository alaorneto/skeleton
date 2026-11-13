"""The organization's admission rules for its SSO providers (#31)."""

import json
from uuid import uuid4

import pytest
from allauth.core import context
from allauth.core.exceptions import ImmediateHttpResponse
from cryptography.fernet import Fernet
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory, override_settings
from django.urls import reverse

from skeleton.accounts import identity_providers
from skeleton.accounts.adapters import SocialAccountAdapter, admit_or_request_social
from skeleton.accounts.providers import admission_for, configured_providers, load_provider_config
from skeleton.accounts.services import bootstrap_identity, create_identity
from skeleton.tenancy.context import request_context, system_context, tenant_context
from tests import support

PASSWORD = "Identity-admission-Password!31"
KEY = Fernet.generate_key().decode()


def empty():
    return load_provider_config(None)


@pytest.fixture
def setup(skeleton_db):
    with override_settings(
        IDENTITY_PROVIDER_ENCRYPTION_KEYS=[KEY],
        SKELETON_PROVIDER_CONFIGURATION=empty(),
        SOCIALACCOUNT_PROVIDERS=empty().settings,
    ):
        owner_a, org_a = bootstrap_identity(
            email="ida-owner-a@example.test", organization="ida-a", name="IdA A", password=PASSWORD
        )
        owner_b, org_b = bootstrap_identity(
            email="ida-owner-b@example.test", organization="ida-b", name="IdA B", password=PASSWORD
        )
        with system_context(reason="test:ida-demote"):
            for owner in (owner_a, owner_b):
                owner.is_staff = owner.is_superuser = False
                owner.save(update_fields=["is_staff", "is_superuser"])
        with system_context(reason="test:ida-provider"):
            row = identity_providers.create_identity_provider(
                actor=None,
                data={
                    "provider_id": "ida-oidc",
                    "protocol": "oidc",
                    "bound_organization": org_a,
                    "issuer": "https://login.example.test/realms/ida",
                    "client_id": "ida-client",
                    "secret": "ida-secret",
                    "trust_verified_email": True,
                },
            )
        yield {"owner_a": owner_a, "a": org_a, "owner_b": owner_b, "b": org_b, "row": row}


def login(user):
    return support.login(user, password=PASSWORD, csrf=False)


def put(client, organization, row, body, revision):
    return client.put(
        reverse("api:organization-identity-provider-admission", args=[organization.pk, row.pk]),
        json.dumps(body),
        content_type="application/json",
        headers={"If-Match": f'"{revision}"'},
    )


def rules(**extra):
    return {
        "jit_provisioning": True,
        "jit_requires_approval": False,
        "default_role": None,
        "allowed_email_domains": [],
        **extra,
    }


def binding():
    with request_context(actor=None, request_id=uuid4(), source="test:ida-binding"):
        return configured_providers().get("ida-oidc")


def test_b_never_sees_a_providers(setup):
    a, b, row = setup["a"], setup["b"], setup["row"]
    client_b = login(setup["owner_b"])
    page = client_b.get(reverse("web:identity", args=[b.pk]))
    assert page.status_code == 200 and page.context["rows"] == [] and b"ida-oidc" not in page.content
    listing = client_b.get(reverse("api:organization-identity-providers", args=[b.pk])).json()["results"]
    assert listing == []
    detail = reverse("api:organization-identity-provider-admission", args=[b.pk, row.pk])
    assert client_b.get(detail).status_code == 404
    assert put(client_b, b, row, rules(), 0).status_code == 404
    client_a = login(setup["owner_a"])
    listing = client_a.get(reverse("api:organization-identity-providers", args=[a.pk])).json()["results"]
    assert [item["provider_id"] for item in listing] == ["ida-oidc"]
    assert "secret" not in json.dumps(listing) and "ida-secret" not in json.dumps(listing)


def test_jit_is_off_until_configured_with_cas(setup):
    a, row = setup["a"], setup["row"]
    assert admission_for(binding()).jit_provisioning is False
    client = login(setup["owner_a"])
    assert put(client, a, row, rules(), 1).status_code == 412
    created = put(client, a, row, rules(allowed_email_domains=["Empresa.COM", "empresa.com"]), 0)
    assert created.status_code == 200, created.content
    assert created.json()["allowed_email_domains"] == ["empresa.com"]
    assert admission_for(binding()).jit_provisioning is True
    assert put(client, a, row, rules(), 0).status_code == 412
    extra = put(client, a, row, {**rules(), "default_role_code": "organization_admin"}, 1)
    assert extra.status_code == 400
    page = client.post(
        reverse("web:identity-admission", args=[a.pk, row.pk]),
        {f"{row.pk}-jit_provisioning": "on", f"{row.pk}-revision": "0"},
    )
    assert page.status_code == 409


def test_role_must_belong_to_the_organization(setup):
    from skeleton.access.models import Role

    a, b, row = setup["a"], setup["b"], setup["row"]
    with tenant_context(organization_id=b.pk, actor=setup["owner_b"], source="test:ida-role-b"):
        foreign = Role.objects.get(code="member", is_builtin=True)
    with tenant_context(organization_id=a.pk, actor=setup["owner_a"], source="test:ida-role-a"):
        manager = Role.objects.get(code="app_manager", is_builtin=True)
    client = login(setup["owner_a"])
    assert put(client, a, row, rules(default_role=str(foreign.pk)), 0).status_code == 400
    # The administrator may choose a role with capabilities.
    assert put(client, a, row, rules(default_role=str(manager.pk)), 0).status_code == 200
    assert admission_for(binding()).default_role_id == manager.pk


def test_jit_with_approval_creates_a_pending_request(setup):
    from skeleton.tenancy.models import JoinRequest, Membership

    a, row = setup["a"], setup["row"]
    client = login(setup["owner_a"])
    assert put(client, a, row, rules(jit_requires_approval=True), 0).status_code == 200
    with system_context(reason="test:ida-person"):
        person = create_identity(email="newcomer@empresa.com", password=None)
    request = admit_or_request_social(person, binding())
    assert request is not None and (request.channel, request.status) == ("sso", "pending")
    with tenant_context(organization_id=a.pk, actor=setup["owner_a"], source="test:ida-check"):
        assert not Membership.objects.filter(user=person).exists()
        assert JoinRequest.objects.filter(user=person, channel="sso").count() == 1
    assert admit_or_request_social(person, binding()).pk == request.pk  # Idempotent while open.
    assert put(client, a, row, rules(), 1).status_code == 200
    with system_context(reason="test:ida-direct"):
        direct = create_identity(email="direct@empresa.com", password=None)
    admit_or_request_social(direct, binding())
    with tenant_context(organization_id=a.pk, actor=setup["owner_a"], source="test:ida-direct-check"):
        assert Membership.objects.filter(user=direct, is_active=True).exists()


def _request():
    req = RequestFactory().get("/accounts/oidc/ida-oidc/login/callback/")
    SessionMiddleware(lambda req: None).process_request(req)
    req.user = AnonymousUser()
    from django.contrib.messages.storage.fallback import FallbackStorage

    req._messages = FallbackStorage(req)
    return req


def test_email_outside_allowed_domains_gets_the_neutral_denial(setup):
    a, row = setup["a"], setup["row"]
    client = login(setup["owner_a"])
    assert put(client, a, row, rules(allowed_email_domains=["empresa.com"]), 0).status_code == 200
    req = _request()
    with request_context(actor=None, request_id=uuid4(), source="test:ida-domains"), context.request_context(req):
        provider = SocialAccountAdapter().get_provider(req, "ida-oidc")
        outside = provider.sociallogin_from_response(
            req, {"sub": "outside-1", "email": "eve@outra.org", "email_verified": True}
        )
        outside.lookup()
        with pytest.raises(ImmediateHttpResponse) as denied:
            SocialAccountAdapter().pre_social_login(req, outside)
        assert denied.value.response.status_code == 403
        assert b"Identity admission denied." in denied.value.response.content
        inside = provider.sociallogin_from_response(
            req, {"sub": "inside-1", "email": "ana@empresa.com", "email_verified": True}
        )
        inside.lookup()
        SocialAccountAdapter().pre_social_login(req, inside)  # No exception.
