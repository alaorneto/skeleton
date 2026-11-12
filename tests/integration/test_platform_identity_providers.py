"""Platform screens of identity providers (#30)."""

import json
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from django.test import Client, override_settings
from django.urls import reverse

from skeleton.accounts import identity_providers
from skeleton.accounts.services import bootstrap_identity
from skeleton.common.outbound_http import OutboundResponse
from skeleton.tenancy.context import request_context, system_context

pytestmark = pytest.mark.usefixtures("skeleton_db")
PASSWORD = "Platform-idp-Password!30"
KEY = Fernet.generate_key().decode()
SECRET = "very-secret-client-value"


@pytest.fixture(autouse=True)
def keys():
    with override_settings(IDENTITY_PROVIDER_ENCRYPTION_KEYS=[KEY]):
        yield


def login(user):
    client = Client(enforce_csrf_checks=True)
    assert client.get(reverse("account_login")).status_code == 200
    response = client.post(
        reverse("account_login"),
        {"login": user.email, "password": PASSWORD, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
    )
    assert response.status_code == 302
    return client


def post(client, url, data):
    return client.post(url, {**data, "csrfmiddlewaretoken": client.cookies["csrftoken"].value})


@pytest.fixture
def setup():
    platform, organization = bootstrap_identity(
        email="idp-platform@example.test", organization="idp-target", name="IdP Target", password=PASSWORD
    )
    outsider, _ = bootstrap_identity(
        email="idp-outsider@example.test", organization="idp-outsider", name="Outsider", password=PASSWORD
    )
    with system_context(reason="test:idp-outsider"):
        outsider.is_staff = outsider.is_superuser = False
        outsider.save(update_fields=["is_staff", "is_superuser"])
    return platform, organization, outsider, login(platform)


def oidc_form(organization, **extra):
    return {
        "protocol": "oidc",
        "provider_id": "target-oidc",
        "name": "Target SSO",
        "bound_organization": str(organization.pk),
        "issuer": "https://login.example.test/realms/target",
        "client_id": "target-client",
        "secret": SECRET,
        "show_on_login": "on",
        **extra,
    }


def test_platform_creates_edits_and_never_renders_secrets(setup):
    from skeleton.accounts.models import IdentityProvider
    from skeleton.audit.models import AuditEvent

    platform, organization, _, client = setup
    create = reverse("platform:identity-provider-create")
    assert client.get(create + "?protocol=oidc").status_code == 200
    response = post(client, create, oidc_form(organization))
    assert response.status_code == 302, response.content
    row = IdentityProvider.objects.get(provider_id="target-oidc")
    detail = reverse("platform:identity-provider", args=[row.pk])
    page = client.get(detail)
    assert page.status_code == 200 and SECRET.encode() not in page.content
    assert b'name="client_id"' not in page.content and b'name="issuer"' not in page.content
    # Saving without a new secret keeps the stored one.
    form = {"name": "Renamed", "enabled": "on", "expected_revision": row.revision}
    assert post(client, detail, form).status_code == 302
    row.refresh_from_db()
    assert row.name == "Renamed" and row.secret.reveal() == SECRET and row.show_on_login is False
    stale = post(client, detail, {**form, "name": "Stale"})
    assert stale.status_code == 409
    rotated = post(
        client,
        detail,
        {"name": "Renamed", "enabled": "on", "secret": "rotated-value", "expected_revision": row.revision},
    )
    assert rotated.status_code == 302 and b"rotated-value" not in rotated.content
    row.refresh_from_db()
    assert row.secret.reveal() == "rotated-value"
    with request_context(actor=platform, request_id=uuid4(), source="test:idp-platform-audit"):
        changes = json.dumps(list(AuditEvent.objects.filter(record_sysid=row.pk).values_list("changes", flat=True)))
    assert SECRET not in changes and "rotated-value" not in changes and "[REDACTED]" in changes


def test_identity_fields_are_refused_if_posted(setup):
    from skeleton.accounts.models import IdentityProvider

    _, organization, _, client = setup
    assert post(client, reverse("platform:identity-provider-create"), oidc_form(organization)).status_code == 302
    row = IdentityProvider.objects.get(provider_id="target-oidc")
    detail = reverse("platform:identity-provider", args=[row.pk])
    for field, value in (("client_id", "hijack"), ("issuer", "https://evil.example.test"), ("provider_id", "x")):
        response = post(client, detail, {"name": "x", "enabled": "on", "expected_revision": row.revision, field: value})
        assert response.status_code == 400
    row.refresh_from_db()
    assert (row.client_id, row.issuer, row.name) == (
        "target-client",
        "https://login.example.test/realms/target",
        "Target SSO",
    )


def test_create_validates_protocol_options(setup):
    _, organization, _, client = setup
    create = reverse("platform:identity-provider-create")
    insecure = post(client, create, oidc_form(organization, issuer="http://login.example.test/realms/x"))
    assert insecure.status_code == 400
    missing_secret = post(client, create, oidc_form(organization, secret=""))
    assert missing_secret.status_code == 400
    saml = {
        "protocol": "saml",
        "provider_id": "target-saml",
        "name": "Target SAML",
        "bound_organization": str(organization.pk),
        "entity_id": "https://saml.example.test/idp",
        "client_id": "target-saml",
        "sp_entity_id": "https://app.example.test/saml",
    }
    assert post(client, create + "?protocol=saml", saml).status_code == 400  # Neither certificate nor metadata.
    saml["idp_x509cert"] = "MIIC-cert"
    assert post(client, create + "?protocol=saml", saml).status_code == 302


def test_guards_csrf_reauthentication_and_outsiders(setup):
    _, organization, outsider, client = setup
    create = reverse("platform:identity-provider-create")
    assert client.post(create, oidc_form(organization)).status_code == 403  # No CSRF token.
    other = login(outsider)
    assert other.get(reverse("platform:identity-providers")).status_code == 403
    assert post(other, create, oidc_form(organization)).status_code == 403
    session = client.session
    session["account_authentication_methods"][-1]["at"] = 0
    session.save()
    response = post(client, create, oidc_form(organization))
    assert response.status_code == 302 and response["Location"].startswith(reverse("account_reauthenticate"))
    from skeleton.accounts.models import IdentityProvider

    assert not IdentityProvider.objects.exists()


def test_check_writes_only_the_last_check(setup, monkeypatch):
    from skeleton.accounts.models import IdentityProvider
    from skeleton.common import outbound_http

    _, organization, _, client = setup
    assert post(client, reverse("platform:identity-provider-create"), oidc_form(organization)).status_code == 302
    row = IdentityProvider.objects.get(provider_id="target-oidc")
    calls = []

    def fake(method, url, **kwargs):
        calls.append(url)
        body = json.dumps({"issuer": "https://login.example.test/realms/target"}).encode()
        return OutboundResponse(status=200, headers={}, body=body, truncated=False)

    monkeypatch.setattr(outbound_http, "request", fake)
    assert post(client, reverse("platform:identity-provider-check", args=[row.pk]), {}).status_code == 302
    after = IdentityProvider.objects.get(pk=row.pk)
    assert calls == ["https://login.example.test/realms/target/.well-known/openid-configuration"]
    assert after.last_check_code == "ok" and after.revision == row.revision + 1
    assert (after.name, after.client_id, after.enabled) == (row.name, row.client_id, row.enabled)

    def unreachable(method, url, **kwargs):
        raise outbound_http.OutboundRequestFailed("timeout")

    monkeypatch.setattr(outbound_http, "request", unreachable)
    post(client, reverse("platform:identity-provider-check", args=[row.pk]), {})
    assert IdentityProvider.objects.get(pk=row.pk).last_check_code == "unreachable"


def test_organization_card_lists_its_providers(setup):
    _, organization, _, client = setup
    assert post(client, reverse("platform:identity-provider-create"), oidc_form(organization)).status_code == 302
    page = client.get(reverse("platform:organization", args=[organization.pk]))
    assert page.status_code == 200
    assert [row.provider_id for row in page.context["identity_providers"]] == ["target-oidc"]
    assert SECRET.encode() not in page.content


def test_form_allowlist_is_enforced_by_the_service():
    with pytest.raises(Exception):  # noqa: B017
        identity_providers.normalize_settings("saml", {"idp": {"x509cert": "c"}, "advanced": {"strict": False}})
