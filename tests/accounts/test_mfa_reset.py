"""Reset of the MFA of an identity that lost its authenticator and recovery codes (#736)."""

from io import StringIO
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from django.core import mail
from django.core.exceptions import PermissionDenied
from django.core.management import CommandError, call_command
from django.test import Client, override_settings
from django.urls import reverse

from skeleton.accounts.models import User
from skeleton.accounts.services import (
    MFAResetRefused,
    bootstrap_identity,
    create_identity,
    create_service_account,
    reset_mfa,
)
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.tenancy.context import request_context, system_context, tenant_context

pytestmark = pytest.mark.usefixtures("skeleton_db")
PASSWORD = "Fixture-mfa-reset-password-2026!736"
SECRET = "JBSWY3DPEHPK3PXP"
KEYS = [Fernet.generate_key().decode()]


@pytest.fixture(autouse=True)
def mfa_keys():
    with override_settings(MFA_ENCRYPTION_KEYS=KEYS):
        yield


@pytest.fixture
def admin():
    user, organization = bootstrap_identity(
        email="mfa-reset-admin@example.test", organization="mfa-reset", name="MFA reset", password=PASSWORD
    )
    user.test_organization_id = organization.pk
    return user


def person(email, *, enrolled=True):
    from allauth.account.models import EmailAddress
    from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
    from allauth.mfa.totp.internal.auth import TOTP

    with system_context(reason="test:mfa-reset-person"):
        user = create_identity(email=email, password=PASSWORD)
        EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
        if enrolled:
            TOTP.activate(user, SECRET)
            codes = RecoveryCodes.activate(user).generate_codes()
            return user, codes
    return user, []


def authenticators(user):
    from allauth.mfa.models import Authenticator

    return Authenticator.objects.filter(user_id=user.pk).count()


def fresh(user):
    return User.all_objects.get(pk=user.pk)


def login(email):
    client = Client()
    assert client.post("/accounts/login/", {"login": email, "password": PASSWORD}).status_code == 302
    return client


def test_platform_admin_resets_mfa_and_the_person_enrolls_again(admin):
    from skeleton.audit.models import AuditEvent

    target, codes = person("mfa-reset-target@example.test")
    assert authenticators(target) == 2
    session = Client()
    session.force_login(target)
    assert session.get("/api/v1/me/").status_code == 200
    page = login(admin.email)
    detail = page.get(reverse("platform:user", args=[target.pk]))
    assert detail.status_code == 200 and reverse("platform:user-mfa-reset", args=[target.pk]) in detail.text
    url = reverse("platform:user-mfa-reset", args=[target.pk])
    confirmation = page.get(url)
    assert confirmation.status_code == 200 and "Aplicativo autenticador" in confirmation.text
    assert authenticators(target) == 2, "GET never resets."
    before = fresh(target)
    stale = page.post(url, {"expected_revision": before.revision - 1})
    assert stale.status_code == 409 and authenticators(target) == 2
    mail.outbox.clear()
    response = page.post(url, {"expected_revision": before.revision})
    assert response.status_code == 302 and response["Location"] == reverse("platform:user", args=[target.pk])
    after = fresh(target)
    assert authenticators(target) == 0
    assert after.mfa_reset_at is not None and after.auth_version == before.auth_version + 1
    assert session.get("/api/v1/me/").status_code in (401, 403), "Every earlier session ends."
    [message] = [message for message in mail.outbox if target.email in message.to]
    assert "MFA" in message.subject and SECRET not in message.body
    assert not any(code in message.body for code in codes)
    with request_context(actor=admin, request_id=uuid4(), source="test:mfa-reset-audit"):
        event = AuditEvent.objects.filter(record_sysid=target.pk, operation="update").order_by("-created_at").first()
    assert event.actor_sysid == admin.pk and set(event.changes) >= {"mfa_reset_at", "auth_version"}
    assert SECRET not in str(event.changes)
    # Without authenticators the password alone signs in, and the person enrolls MFA again.
    again = Client()
    signed_in = again.post("/accounts/login/", {"login": target.email, "password": PASSWORD})
    assert signed_in.status_code == 302 and "/accounts/2fa/" not in signed_in["Location"]
    assert again.get("/api/v1/me/").status_code == 200
    assert again.get("/accounts/2fa/").status_code == 200
    # A second reset finds nothing to remove.
    repeated = page.post(url, {"expected_revision": fresh(target).revision})
    assert repeated.status_code == 409 and "não tem MFA" in repeated.text


def test_reset_page_refuses_everyone_but_the_platform_administrator(admin):
    from skeleton.accounts.platform import grant_platform_role

    target, _ = person("mfa-reset-guarded@example.test")
    auditor, _ = person("mfa-reset-auditor@example.test", enrolled=False)
    member, _ = person("mfa-reset-member@example.test", enrolled=False)
    with request_context(actor=admin, request_id=uuid4(), source="test:mfa-reset-role"):
        grant_platform_role(actor=admin, user=auditor, role="auditor")
    url = reverse("platform:user-mfa-reset", args=[target.pk])
    revision = fresh(target).revision
    anonymous = Client().post(url, {"expected_revision": revision})
    assert anonymous.status_code == 302 and "/accounts/login/" in anonymous["Location"]
    for email in (auditor.email, member.email):
        client = login(email)
        assert client.get(url).status_code == 403
        assert client.post(url, {"expected_revision": revision}).status_code == 403
    strict = Client(enforce_csrf_checks=True)
    assert strict.post("/accounts/login/", {"login": admin.email, "password": PASSWORD}).status_code == 403
    strict.force_login(admin)
    assert strict.post(url, {"expected_revision": revision}).status_code == 403
    own = login(admin.email).get(reverse("platform:user", args=[admin.pk]))
    assert reverse("platform:user-mfa-reset", args=[admin.pk]) not in own.text
    assert authenticators(target) == 2 and fresh(target).revision == revision


def test_reset_service_refusals_change_nothing(admin):
    target, _ = person("mfa-reset-refusals@example.test")
    bare, _ = person("mfa-reset-bare@example.test", enrolled=False)
    other, _ = person("mfa-reset-other@example.test", enrolled=False)
    with tenant_context(
        organization_id=admin.test_organization_id, actor=admin, source="test:mfa-reset-service-account"
    ):
        service = create_service_account(actor=admin, name="MFA reset robot")
    cases = (
        (admin, admin, MFAResetRefused),
        (service, admin, MFAResetRefused),
        (bare, admin, MFAResetRefused),
        (target, other, PermissionDenied),
    )
    for subject, actor, error in cases:
        before = fresh(subject)
        with request_context(actor=actor, request_id=uuid4(), source="test:mfa-reset-refusal"):
            with pytest.raises(error):
                reset_mfa(subject, actor=actor, expected_revision=before.revision)
        assert fresh(subject).revision == before.revision and fresh(subject).mfa_reset_at is None
    with request_context(actor=admin, request_id=uuid4(), source="test:mfa-reset-stale"):
        with pytest.raises(ConcurrentUpdate):
            reset_mfa(target, actor=admin, expected_revision=fresh(target).revision + 1)
    assert authenticators(target) == 2


def test_emergency_command_reports_then_resets(admin):
    from skeleton.audit.models import AuditEvent

    target, _ = person("mfa-reset-command@example.test")
    with pytest.raises(CommandError):
        call_command("reset_mfa", "--email", target.email)
    with pytest.raises(CommandError):
        call_command("reset_mfa", "--email", target.email, "--reason", "   ", "--confirm")
    with pytest.raises(CommandError):
        call_command("reset_mfa", "--email", "nobody@example.test", "--reason", "ticket 1", "--confirm")
    out = StringIO()
    call_command("reset_mfa", "--email", target.email, "--reason", "ticket 42", stdout=out)
    assert "Nothing changed" in out.getvalue() and target.email not in out.getvalue()
    assert authenticators(target) == 2 and fresh(target).mfa_reset_at is None
    out = StringIO()
    mail.outbox.clear()
    call_command("reset_mfa", "--email", target.email.upper(), "--reason", "ticket 42", "--confirm", stdout=out)
    assert f"Identity {target.pk}: 2 authenticator(s) removed" in out.getvalue()
    assert target.email not in out.getvalue() and SECRET not in out.getvalue()
    assert authenticators(target) == 0 and fresh(target).mfa_reset_at is not None
    [message] = [message for message in mail.outbox if target.email in message.to]
    assert "MFA" in message.subject and SECRET not in message.body
    with request_context(actor=admin, request_id=uuid4(), source="test:mfa-reset-command-audit"):
        event = AuditEvent.objects.filter(record_sysid=target.pk, operation="update").order_by("-created_at").first()
    assert event.actor_sysid is None and "mfa:reset ticket 42" in event.source
    with pytest.raises(CommandError):
        call_command("reset_mfa", "--email", target.email, "--reason", "ticket 42", "--confirm")
