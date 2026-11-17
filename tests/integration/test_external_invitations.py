"""External invitations and admission (#532): only the digest stored, the verified email, the app handler,
neutral refusals, limits, revocation and the service contract.

Runs on PostgreSQL with the runtime role.
"""

import re
import uuid
from datetime import timedelta

import pytest
from django.core import mail
from django.core.exceptions import ValidationError
from django.urls import reverse

from skeleton.service_api import Conflict, Forbidden, NotFound, Unavailable
from skeleton.service_api.external import ExternalAccessService
from skeleton.tenancy import external
from skeleton.tenancy.context import system_context
from skeleton.testing.external import enable_portal, external_scenario
from skeleton.testing.service_api import acting_as, assert_service_contract

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]
APP = "example_notes"


@pytest.fixture
def scenario(settings):
    from skeleton.testing.applications import activate_app, open_app

    settings.SKELETON_PUBLIC_URL = "https://portal.example.test"
    found = external_scenario("ext-invite")
    open_app(APP)
    for organization in (found.a, found.b):
        activate_app(APP, organization.pk)
    return found


def verified(user, email=None):
    from allauth.account.models import EmailAddress

    with system_context(reason="test:verified-email"):
        EmailAddress.objects.get_or_create(
            user=user, email=email or user.email, defaults={"verified": True, "primary": True}
        )
    return user


def invite(scenario, *, email=None, subject=None, key=None, organization=None, actor=None):
    organization = organization or scenario.a
    with acting_as(actor or (scenario.admin_a if organization == scenario.a else None), organization) as user:
        return ExternalAccessService().invite(
            actor=user,
            email=email or scenario.outsider.email,
            app_label=APP,
            subject_ref=subject or uuid.UUID(int=1),
            idempotency_key=key or uuid.uuid4().hex,
        )


def token_of(message):
    return re.search(r"/portal/invite/[0-9a-f-]+/([A-Za-z0-9_-]+)/", message.body).group(1)


def last_token(django_capture_on_commit_callbacks, call):
    with django_capture_on_commit_callbacks(execute=True):
        result = call()
    return result, token_of(mail.outbox[-1])


def test_the_invitation_stores_only_the_digest_and_the_email_is_personal(scenario, django_capture_on_commit_callbacks):
    from skeleton.audit.models import AuditEvent
    from skeleton.tenancy.models import ExternalInvitation

    info, token = last_token(django_capture_on_commit_callbacks, lambda: invite(scenario))
    assert info.email == scenario.outsider.email and not info.accepted
    with system_context(reason="test:read", organization_id=scenario.a.pk):
        row = ExternalInvitation.objects.get(pk=info.sysid)
        assert token not in row.token_digest and len(row.token_digest) == 64
        event = AuditEvent.objects.get(record_sysid=info.sysid, operation="create")
    assert event.changes["token_digest"]["new"] == "[REDACTED]"
    assert event.changes["email"]["new"] == "[REDACTED]"


def test_who_may_invite(scenario, monkeypatch):
    with pytest.raises(Forbidden):
        invite(scenario, actor=scenario.member)
    from skeleton.service_api import external as module

    monkeypatch.setattr(module, "_by_app", lambda app_label: app_label == APP)  # The app itself calls.
    assert invite(scenario, actor=scenario.member).app_label == APP
    monkeypatch.undo()
    enable_portal(scenario.a, enabled=False)
    with pytest.raises(Conflict) as error:
        invite(scenario)
    assert error.value.code == "external_disabled"
    enable_portal(scenario.a)
    from skeleton.system.applications import application_state, configure_application

    with acting_as(scenario.admin_a, scenario.a):
        state = application_state(APP)
        configure_application(
            APP, user=scenario.admin_a, enabled=False, configuration=state["configuration"], revision=state["revision"]
        )
    with pytest.raises(Unavailable):
        invite(scenario)


def test_acceptance_creates_the_access_and_runs_the_app_handler(
    scenario, django_capture_on_commit_callbacks, monkeypatch
):
    from skeleton.tenancy.models import ExternalAccess, ExternalInvitation

    calls = []
    monkeypatch.setitem(external._handlers, APP, lambda access, user, subject: calls.append((access, user, subject)))
    info, token = last_token(django_capture_on_commit_callbacks, lambda: invite(scenario, subject=uuid.UUID(int=7)))
    access = external.accept(user=verified(scenario.outsider), organization_id=scenario.a.pk, token=token)
    assert calls == [(access.pk, scenario.outsider.pk, uuid.UUID(int=7))]
    with system_context(reason="test:read", organization_id=scenario.a.pk):
        assert ExternalAccess.objects.get(user=scenario.outsider).is_active
        assert ExternalInvitation.objects.get(pk=info.sysid).access_id == access.pk
    with acting_as(scenario.admin_a, scenario.a) as admin:
        linked = ExternalAccessService().access_for(actor=admin, app_label=APP, subject_ref=uuid.UUID(int=7))
    assert linked.sysid == access.pk


def test_a_failing_handler_undoes_the_acceptance(scenario, django_capture_on_commit_callbacks, monkeypatch):
    from skeleton.tenancy.models import ExternalAccess

    def broken(access, user, subject):
        raise RuntimeError("the app refused")

    monkeypatch.setitem(external._handlers, APP, broken)
    _, token = last_token(django_capture_on_commit_callbacks, lambda: invite(scenario))
    with pytest.raises(RuntimeError):
        external.accept(user=verified(scenario.outsider), organization_id=scenario.a.pk, token=token)
    with system_context(reason="test:read", organization_id=scenario.a.pk):
        assert not ExternalAccess.objects.filter(user=scenario.outsider).exists()
    external.validate(scenario.a.pk, token)  # Still a live invitation.


def test_every_refusal_answers_the_same(scenario, django_capture_on_commit_callbacks):
    from skeleton.accounts.services import create_identity
    from skeleton.tenancy.models import ExternalInvitation

    def refused(user, organization_id, token):
        with pytest.raises(ValidationError) as error:
            external.accept(user=user, organization_id=organization_id, token=token)
        assert error.value.code == "invalid_invitation"

    _, token = last_token(django_capture_on_commit_callbacks, lambda: invite(scenario))
    with system_context(reason="test:other"):
        other = create_identity(email="other@ext-invite.example.test", password="Fixture-only-password-2026!")
    refused(verified(other), scenario.a.pk, token)  # Another email.
    refused(scenario.outsider, scenario.a.pk, token)  # Not verified.
    refused(verified(scenario.outsider), scenario.b.pk, token)  # Another organization.
    refused(scenario.outsider, scenario.a.pk, "x" * 43)  # Unknown token.
    with system_context(reason="test:read", organization_id=scenario.a.pk):
        row = ExternalInvitation.objects.get()
    with acting_as(scenario.admin_a, scenario.a) as admin:
        ExternalAccessService().revoke_invitation(actor=admin, invitation_id=row.pk, expected_revision=row.revision)
    refused(scenario.outsider, scenario.a.pk, token)  # Revoked.
    _, used = last_token(django_capture_on_commit_callbacks, lambda: invite(scenario, subject=uuid.UUID(int=2)))
    external.accept(user=scenario.outsider, organization_id=scenario.a.pk, token=used)
    refused(scenario.outsider, scenario.a.pk, used)  # Already used.


def test_an_expired_invitation_is_refused(scenario, django_capture_on_commit_callbacks, monkeypatch):
    from django.utils import timezone

    _, token = last_token(django_capture_on_commit_callbacks, lambda: invite(scenario))
    later = timezone.now() + timedelta(hours=73)
    monkeypatch.setattr(external.timezone, "now", lambda: later)
    with pytest.raises(ValidationError):
        external.accept(user=verified(scenario.outsider), organization_id=scenario.a.pk, token=token)


def test_a_second_invitation_replaces_the_first_and_the_key_is_idempotent(scenario, django_capture_on_commit_callbacks):
    _, first = last_token(django_capture_on_commit_callbacks, lambda: invite(scenario, key="k1"))
    sent = len(mail.outbox)
    with django_capture_on_commit_callbacks(execute=True):
        again = invite(scenario, key="k1")
    assert len(mail.outbox) == sent  # The same invitation, no new email.
    _, second = last_token(django_capture_on_commit_callbacks, lambda: invite(scenario, key="k2"))
    with pytest.raises(ValidationError):
        external.validate(scenario.a.pk, first)
    assert external.validate(scenario.a.pk, second).sysid != again.sysid


def test_acceptance_attempts_are_limited(scenario, monkeypatch):
    monkeypatch.setattr(external, "ACCEPT_ATTEMPTS_PER_HOUR", 2)
    user = verified(scenario.outsider)
    for _ in range(2):
        with pytest.raises(ValidationError):
            external.accept(user=user, organization_id=scenario.a.pk, token="y" * 43, ip="203.0.113.9")
    with pytest.raises(external.TooManyAttempts):
        external.accept(user=user, organization_id=scenario.a.pk, token="y" * 43, ip="203.0.113.9")


def test_the_plan_limit_refuses_the_acceptance_and_keeps_the_invitation(
    scenario, django_capture_on_commit_callbacks, monkeypatch
):
    from skeleton.common import entitlements
    from skeleton.common.exceptions import QuotaLimitExceeded

    def full(organization_id, metric, increment):
        if metric == "external_people":
            raise QuotaLimitExceeded(metric, 0)

    monkeypatch.setattr(entitlements, "check_limit", full)
    _, token = last_token(django_capture_on_commit_callbacks, lambda: invite(scenario))
    with pytest.raises(QuotaLimitExceeded):
        external.accept(user=verified(scenario.outsider), organization_id=scenario.a.pk, token=token)
    external.validate(scenario.a.pk, token)


def test_revoking_the_access_closes_the_portal_and_keeps_the_rows(scenario):
    from django.core.exceptions import PermissionDenied

    from skeleton.tenancy.context import external_context
    from skeleton.tenancy.models import ExternalAccess

    with acting_as(scenario.admin_a, scenario.a) as admin:
        [access] = [
            item for item in ExternalAccessService().accesses(actor=admin).items if item.user_id == scenario.external.pk
        ]
        with pytest.raises(Conflict):
            ExternalAccessService().revoke(actor=admin, access_id=access.sysid, expected_revision=access.revision + 5)
        revoked = ExternalAccessService().revoke(actor=admin, access_id=access.sysid, expected_revision=access.revision)
    assert not revoked.is_active
    with pytest.raises(PermissionDenied):
        with external_context(organization_id=scenario.a.pk, actor=scenario.external):
            pass
    with system_context(reason="test:read", organization_id=scenario.a.pk):
        assert ExternalAccess.objects.filter(pk=access.sysid).exists()


def test_the_service_contract_isolation_and_the_portal_me(scenario):
    from skeleton.testing.external import acting_external

    assert_service_contract(ExternalAccessService)
    with acting_as(scenario.admin_a, scenario.a) as admin:
        info = ExternalAccessService().invite(
            actor=admin,
            email="x@ext-invite.example.test",
            app_label=APP,
            subject_ref=uuid.UUID(int=3),
            idempotency_key="iso",
        )
        with pytest.raises(Forbidden):
            ExternalAccessService().invite(
                actor=scenario.dual,
                email="x@ext-invite.example.test",
                app_label=APP,
                subject_ref=uuid.UUID(int=3),
                idempotency_key="z",
            )  # Another actor than the context's.
        with pytest.raises(Forbidden):
            ExternalAccessService().me(actor=admin)  # Outside the portal.
    from skeleton.tenancy.models import Membership

    with system_context(reason="test:b-admin", organization_id=scenario.b.pk):
        admin_b = Membership.objects.get().user
    with acting_as(admin_b, scenario.b) as other, pytest.raises(NotFound):
        ExternalAccessService().revoke_invitation(
            actor=other, invitation_id=info.sysid, expected_revision=info.revision
        )
    with acting_external(scenario.external, scenario.a) as person:
        me = ExternalAccessService().me(actor=person)
        assert me.user_id == scenario.external.pk and me.is_active
        with pytest.raises(Forbidden):
            ExternalAccessService().accesses(actor=person)


def test_the_pages(scenario, django_capture_on_commit_callbacks):
    from tests import support

    _, token = last_token(django_capture_on_commit_callbacks, lambda: invite(scenario))
    page = reverse("external-invitation", kwargs={"organization_id": scenario.a.pk, "token": token})
    from django.test import Client

    anonymous = Client()
    assert scenario.outsider.email in anonymous.get(page).content.decode()
    person = support.login(verified(scenario.outsider), password=scenario.password)
    accepted = person.post(page, {"csrfmiddlewaretoken": person.cookies["csrftoken"].value})
    assert accepted.status_code == 200 and "Convite aceito" in accepted.content.decode()
    assert person.get(page).status_code == 400  # Used: the same neutral page.
    admin = support.login(verified(scenario.admin_a), password=scenario.password)
    listing = reverse("web:external", kwargs={"organization_id": scenario.a.pk})
    response = admin.get(listing)
    html = response.content.decode()
    assert response.status_code == 200, (response.status_code, response.get("Location"))
    assert "Pessoas externas" in html and "Revogar" in html
    member = support.login(verified(scenario.member), password=scenario.password)
    assert member.get(listing).status_code in (403, 404)
