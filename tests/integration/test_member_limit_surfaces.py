"""Member usage and limit visible, invitations that reserve a seat, clear refusals (#698).

PostgreSQL with the runtime role. The limit comes from ``SKELETON_ORGANIZATION_MEMBER_LIMIT``
(the instance default of #695). Invitations reserve seats; every entry path refused by the
limit says so (page, API 409 ``quota_exceeded``) or refuses neutrally (domain, SSO), and who
manages members is notified once per day and limit when the last seat is taken.
"""

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from skeleton.accounts.services import bootstrap_identity, create_identity
from skeleton.common.exceptions import QuotaLimitExceeded
from skeleton.service_api.errors import Forbidden
from skeleton.service_api.organizations import OrganizationService
from skeleton.tenancy import admission, domains
from skeleton.tenancy.context import request_context, system_context, tenant_context
from skeleton.testing.people import add_member
from skeleton.testing.service_api import acting_as, assert_service_contract
from tests import support

pytestmark = pytest.mark.usefixtures("skeleton_db")
PASSWORD = "Member-limit-surfaces-Password!698"


@pytest.fixture(autouse=True)
def clean_rate_limit():
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


def identity(email, *, verified=True):
    from allauth.account.models import EmailAddress

    with system_context(reason="test:limit-identity"):
        user = create_identity(email=email, password=PASSWORD)
        EmailAddress.objects.create(user=user, email=email, verified=verified, primary=True)
    return user


def demote(user):
    with system_context(reason="test:limit-demote"):
        user.is_staff = user.is_superuser = False
        user.save(update_fields=["is_staff", "is_superuser"])


@pytest.fixture
def orgs(settings):
    settings.SKELETON_PUBLIC_URL = "https://limits.example.test"
    owner_a, org_a = bootstrap_identity(
        email="limit-owner-a@example.test", organization="limit-a", name="Limit A", password=PASSWORD
    )
    owner_b, org_b = bootstrap_identity(
        email="limit-owner-b@example.test", organization="limit-b", name="Limit B", password=PASSWORD
    )
    demote(owner_a)
    demote(owner_b)
    return {"owner_a": owner_a, "a": org_a, "owner_b": owner_b, "b": org_b}


def role(organization, code="member"):
    from skeleton.access.models import Role

    with system_context(reason="test:limit-role", organization_id=organization.pk):
        return Role.objects.get(code=code, is_builtin=True)


def invite(owner, organization, email):
    from skeleton.tenancy.invitations import issue_invitation

    chosen = role(organization)
    with tenant_context(organization_id=organization.pk, actor=owner, source="test:limit-invite"):
        return issue_invitation(user=owner, email=email, role=chosen)


def later(monkeypatch, days=8):
    """Move the clock of the invitation services forward: invitations of 7 days expire."""
    from types import SimpleNamespace

    from skeleton.tenancy import invitations

    real = timezone.now
    monkeypatch.setattr(
        invitations, "timezone", SimpleNamespace(now=lambda: real() + timedelta(days=days), override=timezone.override)
    )


def test_pending_invitations_count_distinct_live_emails(orgs, monkeypatch):
    from skeleton.tenancy.invitations import pending_invitations
    from skeleton.tenancy.models import Invitation

    owner, a = orgs["owner_a"], orgs["a"]
    first = invite(owner, a, "ana@limits.example.test")
    invite(owner, a, "ana@limits.example.test")  # Same email again: one seat.
    invite(owner, a, "bia@limits.example.test")
    revoked = invite(owner, a, "caio@limits.example.test")
    invite(owner, a, owner.email)  # Someone already active here reserves nothing.
    with tenant_context(organization_id=a.pk, actor=owner, source="test:limit-pending"):
        Invitation.objects.get(pk=revoked.pk).delete()
        assert pending_invitations() == 2
        assert pending_invitations(exclude_email=first.email) == 1
        later(monkeypatch)
        assert pending_invitations() == 0  # Expired invitations free their seats.


def test_invitations_reserve_seats(orgs, settings, monkeypatch):
    from skeleton.tenancy.invitations import accept_invitation

    owner, a = orgs["owner_a"], orgs["a"]
    settings.SKELETON_ORGANIZATION_MEMBER_LIMIT = 3
    add_member(organization=a, user=identity("member@limits.example.test"))  # 2 of 3.
    reserved = invite(owner, a, "invited@limits.example.test")  # The third seat is reserved.
    with pytest.raises(ValidationError) as full:
        invite(owner, a, "late@limits.example.test")
    assert full.value.error_dict["email"][0].code == "quota_exceeded"
    invite(owner, a, "invited@limits.example.test")  # The same email reserves nothing more.
    # The reserved seat is taken by its invitation: the acceptance does not count twice.
    person = identity("invited@limits.example.test")
    with request_context(actor=person, request_id=uuid4(), source="test:limit-accept"):
        proof = support_proof(a, reserved)
        accept_invitation(user=person, organization_id=a.pk, proof=proof)
    # An expired invitation frees its seat.
    settings.SKELETON_ORGANIZATION_MEMBER_LIMIT = 4
    invite(owner, a, "other@limits.example.test")
    with pytest.raises(ValidationError):
        invite(owner, a, "late@limits.example.test")
    later(monkeypatch)
    invite(owner, a, "late@limits.example.test")


def support_proof(organization, invitation):
    """The proof of an invitation as the acceptance reads it (the digest stays in the row)."""
    from skeleton.tenancy.invitations import _validate_reference

    return _validate_reference(organization.pk, invitation.token_digest, invitation.pk)


def open_requests(owner, organization):
    with tenant_context(organization_id=organization.pk, actor=owner, source="test:limit-policy"):
        admission.configure_policy(
            actor=owner, data={"discoverable": True, "join_requests_enabled": True}, expected_revision=0
        )


def submit(user, organization):
    with request_context(actor=user, request_id=uuid4(), source="test:limit-submit"):
        return admission.submit_join_request(user=user, organization_id=organization.pk, message="")


def test_join_request_approval_at_the_limit_stays_pending(orgs, settings):
    from skeleton.tenancy.models import JoinRequest

    owner, a = orgs["owner_a"], orgs["a"]
    open_requests(owner, a)
    record = submit(identity("asker@limits.example.test"), a)
    settings.SKELETON_ORGANIZATION_MEMBER_LIMIT = 1
    client = support.login(owner, password=PASSWORD, csrf=False)
    page = client.post(
        reverse("web:join-request-decide", args=[a.pk, record.pk]),
        {"decision": "approve", "revision": record.revision},
    )
    assert page.status_code == 409
    assert "não tem vagas" in page.content.decode()
    api = client.post(
        reverse("api:organization-join-request", args=[a.pk, record.pk]),
        json.dumps({"decision": "approve"}),
        content_type="application/json",
        headers={"If-Match": f'"{record.revision}"'},
    )
    assert api.status_code == 409 and api.json()["code"] == "quota_exceeded"
    with tenant_context(organization_id=a.pk, actor=owner, source="test:limit-pending-request"):
        assert JoinRequest.objects.get(pk=record.pk).status == "pending"


def test_domain_admission_at_the_limit_refuses_neutrally(orgs, settings, caplog):
    from tests.integration.test_verified_domains import FakeDNS

    owner, a = orgs["owner_a"], orgs["a"]
    member = role(a)
    with tenant_context(organization_id=a.pk, actor=owner, source="test:limit-domain"):
        admission.configure_policy(actor=owner, data={"domain_join_enabled": True}, expected_revision=0)
        record = domains.add_domain(actor=owner, domain="full.example.test", admission="auto", role=member)
    dns = FakeDNS()
    dns.publish(record)
    with tenant_context(organization_id=a.pk, actor=owner, source="test:limit-domain-verify"):
        domains.verify_domain(actor=owner, domain_id=record.pk, resolver=dns)
    settings.SKELETON_ORGANIZATION_MEMBER_LIMIT = 1
    person = identity("ana@full.example.test")
    with caplog.at_level("INFO", logger="skeleton.security"):
        with request_context(actor=None, request_id=uuid4(), source="test:limit-domain-confirm"):
            assert domains.apply_verified_email_admission(person, person.email) == "full"
    events = [r for r in caplog.records if getattr(r, "event", "") == "membership.limit_reached"]
    assert events and events[0].reason_code == "domain"
    assert "ana@full.example.test" not in repr(events[0].__dict__)


def test_sso_jit_at_the_limit_keeps_the_person_out(orgs, settings, monkeypatch, caplog):
    from skeleton.accounts import adapters
    from skeleton.tenancy.models import Membership

    owner, a = orgs["owner_a"], orgs["a"]
    member = role(a)
    monkeypatch.setattr(adapters.SocialAccountAdapter, "_resolve_binding", lambda self, binding: (a, member))
    settings.SKELETON_ORGANIZATION_MEMBER_LIMIT = 1
    person = identity("sso@limits.example.test")
    with caplog.at_level("INFO", logger="skeleton.security"):
        with request_context(actor=None, request_id=uuid4(), source="test:limit-sso"):
            assert adapters.provision_social_membership(person, binding=None) is None
    with tenant_context(organization_id=a.pk, actor=owner, source="test:limit-sso-check"):
        assert not Membership.all_objects.filter(user=person).exists()
    events = [r for r in caplog.records if getattr(r, "event", "") == "membership.limit_reached"]
    assert events and events[0].reason_code == "sso"
    assert "sso@limits.example.test" not in repr(events[0].__dict__)


def test_limits_service_and_api(orgs, settings):
    owner, a, b = orgs["owner_a"], orgs["a"], orgs["b"]
    assert_service_contract(OrganizationService)
    plain = identity("plain@limits.example.test")
    add_member(organization=a, user=plain)
    with acting_as(owner, a) as actor:
        assert OrganizationService().limits(actor=actor) == ()  # No default: nothing is limited.
    settings.SKELETON_ORGANIZATION_MEMBER_LIMIT = 3
    invite(owner, a, "reserved@limits.example.test")
    with acting_as(owner, a) as actor:
        [members] = OrganizationService().limits(actor=actor)
    assert (members.metric, members.used, members.limit, members.reserved, members.over_limit) == (
        "members",
        2,
        3,
        1,
        False,
    )
    with acting_as(plain, a) as actor:
        with pytest.raises(Forbidden):
            OrganizationService().limits(actor=actor)

    url = reverse("api:limits", args=[a.pk])
    response = support.login(owner, password=PASSWORD, csrf=False).get(url)
    assert response.status_code == 200
    assert response.json() == {
        "results": [{"metric": "members", "used": 2, "limit": 3, "reserved": 1, "over_limit": False}]
    }
    assert support.login(plain, password=PASSWORD, csrf=False).get(url).status_code == 403
    # Not a member of A: the organization does not exist for B's owner.
    assert support.login(orgs["owner_b"], password=PASSWORD, csrf=False).get(url).status_code == 404
    other = support.login(orgs["owner_b"], password=PASSWORD, csrf=False).get(reverse("api:limits", args=[b.pk]))
    assert other.json()["results"][0]["used"] == 1  # B sees only its own usage.


def test_pages_show_usage_and_disable_invite_when_full(orgs, settings):
    owner, a = orgs["owner_a"], orgs["a"]
    client = support.login(owner, password=PASSWORD, csrf=False)
    invitations = reverse("web:access-list", args=[a.pk, "invitations"])
    assert client.get(invitations).context["meter"] is None  # Unlimited: no meter.
    settings.SKELETON_ORGANIZATION_MEMBER_LIMIT = 3
    add_member(organization=a, user=identity("seat@limits.example.test"))
    page = client.get(invitations)
    meter = page.context["meter"]
    assert (meter["used"], meter["limit"], meter["reserved"], meter["full"]) == (2, 3, 0, False)
    assert "2 de 3 membros" in page.content.decode()
    invite(owner, a, "last@limits.example.test")
    full = client.get(invitations)
    assert full.context["meter"]["full"] and "disabled" in full.content.decode()
    memberships = client.get(reverse("web:access-list", args=[a.pk, "memberships"]))
    assert memberships.context["meter"]["reserved"] == 1
    settings.SKELETON_ORGANIZATION_MEMBER_LIMIT = 1
    over = client.get(memberships.wsgi_request.path)
    assert over.context["meter"]["over_limit"] and "novas entradas estão bloqueadas" in over.content.decode()
    queue = client.get(reverse("web:join-requests", args=[a.pk]))
    assert queue.context["meter"]["limit"] == 1
    # The invitation form refuses with the limit message (409).
    created = client.post(
        reverse("web:access-create", args=[a.pk, "invitations"]),
        {"email": "more@limits.example.test", "role": str(role(a).pk)},
    )
    assert created.status_code == 409


def test_reaching_the_limit_notifies_once_per_day_and_limit(orgs, settings):
    from skeleton.notifications.models import Notification
    from skeleton.tenancy.limit_notices import MEMBER_LIMIT_REACHED
    from skeleton.tenancy.models import Membership

    owner, a, b = orgs["owner_a"], orgs["a"], orgs["b"]
    settings.SKELETON_ORGANIZATION_MEMBER_LIMIT = 2
    ana = add_member(organization=a, user=identity("full-ana@limits.example.test"))  # 2 of 2.

    def sent(organization):
        with system_context(reason="test:limit-notified", organization_id=organization.pk):
            return list(Notification.objects.filter(type_code=MEMBER_LIMIT_REACHED).values_list("payload", flat=True))

    assert sent(a) == [{"limit": 2, "used": 2}]
    from skeleton.access.services import delete_membership

    with acting_as(owner, a) as actor:
        delete_membership(Membership.objects.get(pk=ana.pk), user=actor)
    add_member(organization=a, user=identity("full-bia@limits.example.test"))  # Full again, same day.
    assert len(sent(a)) == 1
    assert sent(b) == []
    with pytest.raises(QuotaLimitExceeded):
        add_member(organization=a, user=identity("full-caio@limits.example.test"))
