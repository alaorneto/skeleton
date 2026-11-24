"""Recent-authentication rule shared by platform mutations, social connect and invitations."""

import re
from urllib.parse import urlsplit

import pytest
from allauth.account.models import EmailAddress
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse

from skeleton.accounts.reauthentication import has_recent_authentication
from skeleton.accounts.services import bootstrap_identity, create_identity
from skeleton.tenancy.context import system_context, tenant_context
from tests import support

PASSWORD = "Recent-authentication-password!57"
pytestmark = pytest.mark.usefixtures("skeleton_db")


def local_identity(email):
    with system_context(reason="test:recent-authentication"):
        user = create_identity(email=email, password=PASSWORD)
        EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    return user


def login(user):
    return support.login(user, password=PASSWORD, csrf=False)


def expire(client):
    session = client.session
    session["account_authentication_methods"][-1]["at"] = 0
    session.save()


def request_for(client, user):
    request = RequestFactory().get("/")
    request.user = user
    request.session = client.session
    return request


def test_login_counts_until_the_timeout_and_only_for_the_same_user():
    user, other = local_identity("recent@example.test"), local_identity("other@example.test")
    client = login(user)
    assert has_recent_authentication(request_for(client, user))
    assert not has_recent_authentication(request_for(client, other))
    expire(client)
    assert not has_recent_authentication(request_for(client, user))
    assert client.post(reverse("account_reauthenticate"), {"password": PASSWORD}).status_code == 302
    assert has_recent_authentication(request_for(client, user))


def test_sso_only_fallback_applies_only_when_requested():
    with system_context(reason="test:recent-authentication-sso"):
        user = create_identity(email="sso-only@example.test", local_password_allowed=False)
    client = Client()
    client.force_login(user)
    request = request_for(client, user)
    assert not user.has_usable_password()
    assert not has_recent_authentication(request)
    assert has_recent_authentication(request, allow_without_reauthentication_flow=True)


def test_platform_mutation_accepts_a_recent_login_and_asks_again_after_the_timeout():
    admin, _ = bootstrap_identity(
        email="recent-platform@example.test", organization="recent-platform", name="Recent", password=PASSWORD
    )
    client = login(admin)

    def upload():
        return client.post("/platform/updates/upload/", {"package": SimpleUploadedFile("app.skapp", b"123456789")})

    with override_settings(SKELETON_PACKAGE_MAX_BYTES=8):
        assert upload().status_code == 413
        expire(client)
        response = upload()
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("account_reauthenticate"))
        assert client.post(reverse("account_reauthenticate"), {"password": PASSWORD}).status_code == 302
        assert upload().status_code == 413


def test_existing_account_accepts_invitation_only_with_recent_authentication():
    from skeleton.access.models import Role
    from skeleton.tenancy.invitations import issue_invitation
    from skeleton.tenancy.models import Membership

    owner, organization = bootstrap_identity(
        email="recent-owner@example.test", organization="recent-invite", name="Recent invite", password=PASSWORD
    )
    invited = local_identity("recent-invited@example.test")
    request = RequestFactory().get("/", HTTP_HOST="testserver")
    with tenant_context(organization_id=organization.pk, actor=owner, source="test:recent-invite"):
        issue_invitation(user=owner, email=invited.email, role=Role.objects.get(code="member"), request=request)
    message = next(message for message in mail.outbox if invited.email in message.to)
    link = next(link for link in re.findall(r"https?://[^\s<>\"]+", message.body) if "/invite/" in link)
    path = urlsplit(link.rstrip(".,)")).path
    client = login(invited)
    expire(client)
    response = client.post(path)
    assert response["Location"].startswith(reverse("account_reauthenticate"))
    with system_context(reason="test:recent-invite-check", organization_id=organization.pk):
        assert not Membership.objects.filter(user=invited).exists()
    assert client.post(reverse("account_reauthenticate"), {"password": PASSWORD}).status_code == 302
    response = client.post(path)
    assert response["Location"] == reverse("web:dashboard", kwargs={"organization_id": organization.pk})
    with system_context(reason="test:recent-invite-result", organization_id=organization.pk):
        assert Membership.objects.filter(user=invited).count() == 1


def test_mfa_enrolled_person_reauthenticates_with_mfa_not_the_password(subtests):
    """#818: the authenticator proves the device; with MFA enrolled the password does not confirm."""
    import time

    from allauth.mfa import app_settings
    from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
    from allauth.mfa.totp.internal.auth import TOTP, format_hotp_value, hotp_value, validate_totp_code
    from cryptography.fernet import Fernet

    secret = "JBSWY3DPEHPK3PXP"
    admin, _ = bootstrap_identity(
        email="recent-mfa@example.test", organization="recent-mfa", name="Recent MFA", password=PASSWORD
    )
    plain = local_identity("recent-no-mfa@example.test")
    with override_settings(MFA_ENCRYPTION_KEYS=[Fernet.generate_key()], SKELETON_PACKAGE_MAX_BYTES=8):
        with system_context(reason="test:recent-mfa-enrollment"):
            TOTP.activate(admin, secret)
            codes = RecoveryCodes.activate(admin).generate_codes()
        client = Client()
        challenge = client.post(reverse("account_login"), {"login": admin.email, "password": PASSWORD})["Location"]
        # A recovery code at login, so the authenticator code below is not refused as already used.
        assert client.post(challenge, {"code": codes[1]}).status_code == 302

        def upload():
            return client.post("/platform/updates/upload/", {"package": SimpleUploadedFile("app.skapp", b"123456789")})

        with subtests.test("a sensitive action goes to the MFA page"):
            expire(client)
            response = upload()
            assert response.status_code == 302
            assert response["Location"].startswith(reverse("mfa_reauthenticate"))
        with subtests.test("the password page refuses and sends to MFA"):
            response = client.post(reverse("account_reauthenticate"), {"password": PASSWORD})
            assert response.status_code == 302 and response["Location"].startswith(reverse("mfa_reauthenticate"))
            assert not has_recent_authentication(request_for(client, admin))
        with subtests.test("a wrong code is refused"):
            wrong = "000000"
            while validate_totp_code(secret, wrong):
                wrong = f"{(int(wrong) + 1) % 1000000:06d}"
            assert client.post(reverse("mfa_reauthenticate"), {"code": wrong}).status_code == 200
            assert not has_recent_authentication(request_for(client, admin))
        with subtests.test("a recovery code confirms"):
            assert client.post(reverse("mfa_reauthenticate"), {"code": codes[0]}).status_code == 302
            assert upload().status_code == 413
        with subtests.test("the authenticator code confirms and the action resumes"):
            expire(client)
            assert upload()["Location"].startswith(reverse("mfa_reauthenticate"))
            code = format_hotp_value(hotp_value(secret, int(time.time()) // app_settings.TOTP_PERIOD))
            response = client.post(reverse("mfa_reauthenticate"), {"code": code})
            assert response.status_code == 302
            assert upload().status_code == 413
        with subtests.test("without MFA the password still confirms"):
            other = login(plain)
            expire(other)
            response = other.post(reverse("account_reauthenticate"), {"password": PASSWORD})
            assert response.status_code == 302
            assert has_recent_authentication(request_for(other, plain))
