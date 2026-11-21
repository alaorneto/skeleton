"""The core as OIDC provider for native clients (#603): flow, refusals, invalidation, MFA and command."""

from io import StringIO
from uuid import uuid4

import pytest
from django.core.management import CommandError, call_command
from django.test import Client
from django.urls import reverse

from skeleton.tenancy.context import request_context, system_context
from tests import oidc_support as oidc
from tests import support
from tools.security_fixtures import build_security_fixture

pytestmark = pytest.mark.needs_example_app


@pytest.fixture
def provider(skeleton_db, settings):
    oidc.enable_provider(settings)
    access = build_security_fixture()
    oidc.register()
    return access


def session_of(user):
    return support.login(user, password=oidc.PASSWORD, csrf=False)


def test_discovery_publishes_the_canonical_endpoints(provider, settings, subtests):
    with subtests.test("request origin behind the trusted proxy"):
        settings.SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
        data = (
            Client()
            .get("/.well-known/openid-configuration", HTTP_X_FORWARDED_PROTO="https", HTTP_HOST="testserver")
            .json()
        )
        assert data["issuer"] == "https://testserver"
        for name in ("authorization_endpoint", "token_endpoint", "revocation_endpoint", "jwks_uri"):
            assert data[name].startswith("https://testserver/"), name
        assert data["code_challenge_methods_supported"] == ["S256"]
        assert data["grant_types_supported"] == ["authorization_code", "refresh_token"]
        assert "device_authorization_endpoint" not in data
    with subtests.test("public URL wins"):
        settings.SKELETON_PUBLIC_URL = "https://app.example.test"
        data = Client().get("/.well-known/openid-configuration").json()
        assert data["issuer"] == "https://app.example.test"
        assert data["token_endpoint"] == "https://app.example.test/identity/o/api/token"
        assert data["revocation_endpoint"] == "https://app.example.test/identity/o/api/revoke"
        assert data["jwks_uri"] == "https://app.example.test/.well-known/jwks.json"
        assert Client().get("/.well-known/jwks.json").json()["keys"]
    with subtests.test("no key: no provider"):
        settings.IDP_OIDC_PRIVATE_KEY = ""
        assert Client().get("/.well-known/openid-configuration").status_code == 404
        assert Client().get(reverse("oidc-authorize")).status_code == 404


def test_full_flow_refresh_rotation_and_revocation(provider):
    session = session_of(provider.alice)
    verifier, challenge = oidc.pkce()
    for port in (53682, 1024):
        response = session.get(reverse("oidc-authorize"), oidc.authorize_params(challenge, port=port))
        code = oidc.code_from(response)
        assert code and response["Location"].startswith(f"http://127.0.0.1:{port}/callback?")
    tokens = oidc.exchange(code, verifier, port=1024).json()
    assert {"access_token", "refresh_token", "id_token"} <= set(tokens)
    info = Client().get(reverse("oidc-userinfo"), HTTP_AUTHORIZATION=f"Bearer {tokens['access_token']}")
    assert info.status_code == 200 and info.json()["email"] == provider.alice.email
    renewed = oidc.refresh(tokens["refresh_token"])
    assert renewed.status_code == 200
    renewed = renewed.json()
    assert renewed["refresh_token"] != tokens["refresh_token"]
    assert oidc.refresh(tokens["refresh_token"]).json()["error"] == "invalid_grant"  # The old one is gone.
    revoked = Client().post(reverse("oidc-revoke"), {"token": renewed["refresh_token"], "client_id": oidc.CLIENT_ID})
    assert revoked.status_code == 200
    # Revoking the refresh token ends the whole grant, its access tokens included.
    after = Client().get(reverse("oidc-userinfo"), HTTP_AUTHORIZATION=f"Bearer {renewed['access_token']}")
    assert after.status_code == 401
    assert oidc.refresh(renewed["refresh_token"]).json()["error"] == "invalid_grant"


def test_refused_authorizations_and_grants(provider, subtests):
    session = session_of(provider.alice)
    verifier, challenge = oidc.pkce()
    authorize = reverse("oidc-authorize")
    cases = {
        "plain": {"code_challenge_method": "plain", "code_challenge": verifier[:43]},
        "missing pkce": {"code_challenge": None, "code_challenge_method": None},
        "missing method": {"code_challenge_method": None},
        "other path": {"redirect_uri": "http://127.0.0.1:53682/other"},
        "localhost": {"redirect_uri": "http://localhost:53682/callback"},
        "not loopback": {"redirect_uri": "https://evil.example.test/callback"},
        "unknown client": {"client_id": "nobody-desktop"},
        "token response": {"response_type": "token"},
    }
    for name, overrides in cases.items():
        with subtests.test(name):
            response = session.get(authorize, oidc.authorize_params(challenge, **overrides))
            assert oidc.code_from(response) is None
            if response.status_code == 302:  # A registered redirect gets the OAuth error, never a code.
                assert "error=invalid_request" in response["Location"]
            else:
                assert response.status_code == 400
    for grant in ("client_credentials", "password", "urn:ietf:params:oauth:grant-type:device_code"):
        with subtests.test(grant):
            response = Client().post(reverse("oidc-token"), {"grant_type": grant, "client_id": oidc.CLIENT_ID})
            assert response.status_code == 400 and response.json()["error"] == "unsupported_grant_type"
    with subtests.test("device flow does not exist"):
        assert Client().post("/identity/o/api/device/code", {"client_id": oidc.CLIENT_ID}).status_code == 404


@pytest.mark.parametrize("change", ["password", "mfa", "deactivate", "auth_version"])
def test_identity_changes_invalidate_every_token(provider, change):
    from allauth.mfa.models import Authenticator

    from skeleton.accounts.services import revoke_sessions, set_local_password

    tokens = oidc.sign_in(session_of(provider.alice))
    user = provider.alice
    with system_context(reason="test:identity-change"):
        if change == "password":
            set_local_password(user, "Another-fixture-password-2026!")
        elif change == "mfa":
            from allauth.mfa import signals

            Authenticator.objects.create(user=user, type=Authenticator.Type.RECOVERY_CODES, data={"seed": "x"})
            signals.authenticator_added.send(sender=Authenticator, request=None, user=user, authenticator=None)
        elif change == "deactivate":
            from skeleton.accounts.models import User

            user = User.all_objects.get(pk=user.pk)
            user.is_active = False
            user.auth_version += 1
            user.save(update_fields=["is_active", "auth_version"])
        else:
            revoke_sessions(user)
    info = Client().get(reverse("oidc-userinfo"), HTTP_AUTHORIZATION=f"Bearer {tokens['access_token']}")
    assert info.status_code == 401
    assert oidc.refresh(tokens["refresh_token"]).json()["error"] == "invalid_grant"


def test_mfa_is_required_before_a_code(provider, settings):
    from allauth.mfa.models import Authenticator

    with system_context(reason="test:totp"):
        Authenticator.objects.create(user=provider.alice, type=Authenticator.Type.TOTP, data={"secret": "x"})
    session = Client()
    login = session.post(reverse("account_login"), {"login": provider.alice.email, "password": oidc.PASSWORD})
    assert login.status_code == 302 and reverse("mfa_authenticate") in login["Location"]
    _, challenge = oidc.pkce()
    pending = session.get(reverse("oidc-authorize"), oidc.authorize_params(challenge))
    assert oidc.code_from(pending) is None and reverse("account_login") in pending["Location"]
    # A platform administrator without TOTP, under the administrative MFA policy, enrols first.
    settings.SKELETON_REQUIRE_ADMIN_MFA = True
    with system_context(reason="test:platform-admin"):
        provider.admin.is_staff = provider.admin.is_superuser = True
        provider.admin.save(update_fields=["is_staff", "is_superuser"])
    admin = session_of(provider.admin)
    response = admin.get(reverse("oidc-authorize"), oidc.authorize_params(challenge))
    assert response.status_code == 302 and response["Location"] == reverse("mfa_index")


def test_connected_devices_page_lists_and_revokes(provider):
    session = session_of(provider.alice)
    tokens = oidc.sign_in(session)
    page = session.get(reverse("connected-devices"))
    assert page.status_code == 200 and "Acme Desktop" in page.content.decode()
    other = session_of(provider.bob)
    (device,) = page.context["devices"]
    assert other.post(device["revoke_url"]).status_code == 302  # Bob revokes nothing of Alice's.
    assert oidc.refresh(tokens["refresh_token"]).status_code == 200
    renewed = session.get(reverse("connected-devices")).context["devices"]
    assert len(renewed) == 1 and renewed[0]["grant"] == device["grant"]  # The grant survives the rotation.
    assert session.post(renewed[0]["revoke_url"]).status_code == 302
    assert session.get(reverse("connected-devices")).context["devices"] == []
    info = Client().get(reverse("oidc-userinfo"), HTTP_AUTHORIZATION=f"Bearer {tokens['access_token']}")
    assert info.status_code == 401


def test_command_registers_lists_revokes_and_audits(skeleton_db, settings):
    from allauth.idp.oidc.models import Client as OIDCClient
    from allauth.idp.oidc.models import Token

    from skeleton.accounts.models import NativeClient
    from skeleton.audit.models import AuditEvent

    oidc.enable_provider(settings)
    access = build_security_fixture()
    out = StringIO()
    call_command(
        "native_client",
        "register",
        "--client-id",
        "acme-desktop",
        "--name",
        "Acme Desktop",
        "--redirect-uri",
        "http://127.0.0.1/callback",
        "--skip-consent",
        stdout=out,
    )
    client = OIDCClient.objects.get(id="acme-desktop")
    assert (
        client.type == "public"
        and client.skip_consent
        and client.get_grant_types()
        == [
            "authorization_code",
            "refresh_token",
        ]
    )
    with pytest.raises(CommandError):
        call_command(
            "native_client",
            "register",
            "--client-id",
            "acme-desktop",
            "--name",
            "x",
            "--redirect-uri",
            "http://127.0.0.1/callback",
        )
    for bad in ("http://localhost/callback", "http://127.0.0.1:8080/callback", "https://app.example.test/cb"):
        with pytest.raises(CommandError):
            call_command("native_client", "register", "--client-id", "other-app", "--name", "x", "--redirect-uri", bad)
    listing = StringIO()
    call_command("native_client", "list", stdout=listing)
    assert "acme-desktop\tAcme Desktop\tenabled\tskip-consent" in listing.getvalue()
    tokens = oidc.sign_in(session_of(access.alice))
    assert Token.objects.filter(client_id="acme-desktop").exists()
    call_command("native_client", "revoke-all", "--client-id", "acme-desktop", "--disable", stdout=StringIO())
    assert not Token.objects.filter(client_id="acme-desktop").exists()
    assert oidc.refresh(tokens["refresh_token"]).json()["error"] == "invalid_client"
    record = NativeClient.objects.get(client_id="acme-desktop")
    assert record.enabled is False and record.tokens_revoked_at is not None
    with system_context(reason="test:admin"):
        access.admin.is_staff = access.admin.is_superuser = True
        access.admin.save(update_fields=["is_staff", "is_superuser"])
    with request_context(actor=access.admin, request_id=uuid4(), source="test:audit"):
        operations = list(
            AuditEvent.objects.filter(record_sysid=record.pk).order_by("created_at").values_list("operation", "source")
        )
    assert [operation for operation, _ in operations] == ["create", "update"]
    assert {source for _, source in operations} == {"system:native-client:register", "system:native-client:revoke"}
