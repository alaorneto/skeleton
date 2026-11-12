"""Admission by verified email domain (#28)."""

import time
from uuid import uuid4

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse

from skeleton.accounts.services import bootstrap_identity, create_identity
from skeleton.tenancy import admission, domains
from skeleton.tenancy.context import request_context, system_context, tenant_context

pytestmark = pytest.mark.usefixtures("skeleton_db")
PASSWORD = "Verified-domains-Password!28"


class FakeDNS:
    def __init__(self):
        self.records = {}
        self.fail = None

    def __call__(self, name):
        if self.fail:
            raise LookupError(self.fail)
        return self.records.get(name, [])

    def publish(self, record):
        self.records[domains.CHALLENGE_PREFIX + record.domain] = [record.challenge]


def identity(email, *, verified=True):
    from allauth.account.models import EmailAddress

    with system_context(reason="test:domain-identity"):
        user = create_identity(email=email, password=PASSWORD)
        EmailAddress.objects.create(user=user, email=email, verified=verified, primary=True)
    return user


def demote(user):
    with system_context(reason="test:domain-demote"):
        user.is_staff = user.is_superuser = False
        user.save(update_fields=["is_staff", "is_superuser"])


def role(owner, organization, code="member"):
    from skeleton.access.models import Role

    with tenant_context(organization_id=organization.pk, actor=owner, source="test:domain-role"):
        return Role.objects.get(code=code, is_builtin=True)


@pytest.fixture
def orgs():
    owner_a, org_a = bootstrap_identity(
        email="domain-owner-a@example.test", organization="domain-a", name="Domain A", password=PASSWORD
    )
    owner_b, org_b = bootstrap_identity(
        email="domain-owner-b@example.test", organization="domain-b", name="Domain B", password=PASSWORD
    )
    demote(owner_a)
    demote(owner_b)
    return {"owner_a": owner_a, "a": org_a, "owner_b": owner_b, "b": org_b}


def enable(owner, organization, on=True):
    with tenant_context(organization_id=organization.pk, actor=owner, source="test:domain-policy"):
        current = admission.current_policy()
        admission.configure_policy(
            actor=owner, data={"domain_join_enabled": on}, expected_revision=admission.revision_of(current)
        )


def add(owner, organization, domain, *, mode="auto"):
    chosen = role(owner, organization)
    with tenant_context(organization_id=organization.pk, actor=owner, source="test:domain-add"):
        return domains.add_domain(actor=owner, domain=domain, admission=mode, role=chosen)


def verify(owner, organization, record, dns):
    with tenant_context(organization_id=organization.pk, actor=owner, source="test:domain-verify"):
        return domains.verify_domain(actor=owner, domain_id=record.pk, resolver=dns)


def admit(user, email=None):
    with request_context(actor=None, request_id=uuid4(), source="test:domain-confirm"):
        return domains.apply_verified_email_admission(user, email or user.email)


def member_roles(owner, organization, user):
    from skeleton.access.models import MemberRole
    from skeleton.tenancy.models import Membership

    with tenant_context(organization_id=organization.pk, actor=owner, source="test:domain-check"):
        rows = list(Membership.objects.filter(user=user, is_active=True))
        return sorted(MemberRole.objects.filter(membership__in=rows).values_list("role__code", flat=True))


def test_domains_are_normalized_to_idna():
    assert domains.normalize_domain(" Exämple.COM. ") == "xn--exmple-cua.com"
    assert domains.normalize_domain("sub.empresa.com.br") == "sub.empresa.com.br"
    for bad in ("localhost", "*.empresa.com", "a@b.com", "-x.com", "x..com", "1.2.3.4", "a" * 64 + ".com", 42):
        with pytest.raises(ValidationError):
            domains.normalize_domain(bad)
    assert domains.email_domain("Ana@Empresa.COM") == "empresa.com"


def test_pending_or_failed_domain_admits_nobody(orgs):
    owner, a = orgs["owner_a"], orgs["a"]
    enable(owner, a)
    dns = FakeDNS()
    record = add(owner, a, "pending.example.test")
    person = identity("ana@pending.example.test")
    assert admit(person) == "none"
    assert verify(owner, a, record, dns).status == "pending"  # No TXT yet.
    assert admit(person) == "none" and member_roles(owner, a, person) == []


def test_verified_domain_admits_or_asks_and_policy_switch_applies(orgs):
    owner, a = orgs["owner_a"], orgs["a"]
    dns = FakeDNS()
    record = add(owner, a, "auto.example.test")
    dns.publish(record)
    assert verify(owner, a, record, dns).status == "verified"
    early = identity("early@auto.example.test")
    assert admit(early) == "none"  # Domain admission still off in the policy.
    enable(owner, a)
    assert admit(early) == "admitted" and member_roles(owner, a, early) == ["member"]
    assert admit(early) == "member"
    approval = add(owner, a, "ask.example.test", mode="approval")
    dns.publish(approval)
    verify(owner, a, approval, dns)
    asker = identity("asker@ask.example.test")
    assert admit(asker) == "requested" and member_roles(owner, a, asker) == []
    assert admit(asker) == "requested"  # Idempotent while open.
    unverified = identity("late@auto.example.test", verified=False)
    assert admit(unverified) == "none"


def test_domain_claimed_by_a_is_neutral_for_b_and_never_leads_to_b(orgs, monkeypatch):
    owner_a, a, owner_b, b = orgs["owner_a"], orgs["a"], orgs["owner_b"], orgs["b"]
    enable(owner_a, a)
    enable(owner_b, b)
    dns = FakeDNS()
    mine = add(owner_a, a, "shared.example.test")
    dns.publish(mine)
    verify(owner_a, a, mine, dns)
    theirs = add(owner_b, b, "shared.example.test")
    dns.publish(theirs)
    with pytest.raises(domains.DomainUnavailable):
        verify(owner_b, b, theirs, dns)
    person = identity("bob@shared.example.test")
    assert admit(person) == "admitted"
    assert member_roles(owner_a, a, person) == ["member"] and member_roles(owner_b, b, person) == []
    client = Client()
    assert client.post(reverse("account_login"), {"login": owner_b.email, "password": PASSWORD}).status_code == 302
    monkeypatch.setattr(domains, "dns_txt", dns)
    page = client.get(reverse("web:verified-domains", args=[b.pk]))
    assert page.status_code == 200 and b"shared.example.test" in page.content
    response = client.post(reverse("web:verified-domain-action", args=[b.pk, theirs.pk]), {"action": "verify"})
    assert response.status_code == 409 and "Não foi possível verificar este domínio" in response.content.decode()


def test_former_member_is_never_readmitted_by_domain(orgs):
    from skeleton.access import services as access_services
    from skeleton.access.models import MemberRole
    from skeleton.tenancy.models import Membership

    owner, a = orgs["owner_a"], orgs["a"]
    enable(owner, a)
    dns = FakeDNS()
    record = add(owner, a, "former.example.test")
    dns.publish(record)
    verify(owner, a, record, dns)
    person = identity("carla@former.example.test")
    with tenant_context(organization_id=a.pk, actor=owner, source="test:domain-member"):
        membership = Membership.objects.create(organization=a, user=person)
        MemberRole.objects.create(organization=a, membership=membership, role=role(owner, a))
    with tenant_context(organization_id=a.pk, actor=person, source="test:domain-leave"):
        access_services.leave_organization(person)
    from allauth.account.models import EmailAddress

    with system_context(reason="test:domain-second-email"):
        EmailAddress.objects.create(user=person, email="carla2@former.example.test", verified=True, primary=False)
    assert admit(person, "carla2@former.example.test") == "requested"
    assert member_roles(owner, a, person) == []


def test_recheck_drops_the_claim_after_three_failures(orgs):
    from django.core.management import call_command

    owner, a = orgs["owner_a"], orgs["a"]
    enable(owner, a)
    dns = FakeDNS()
    record = add(owner, a, "flaky.example.test")
    dns.publish(record)
    verify(owner, a, record, dns)
    dns.fail = "timeout"
    for expected in (1, 2):
        report = domains.recheck(resolver=dns)
        assert (report.failures, report.revoked) == (1, 0)
        with tenant_context(organization_id=a.pk, actor=owner, source="test:domain-recheck"):
            assert domains.domains().get().failure_count == expected
    assert domains.recheck(resolver=dns).revoked == 1
    with tenant_context(organization_id=a.pk, actor=owner, source="test:domain-failed"):
        assert domains.domains().get().status == "failed"
    assert admit(identity("dora@flaky.example.test")) == "none"
    # A recovered record does not resurrect the claim: the organization verifies again.
    dns.fail = None
    out = __import__("io").StringIO()
    call_command("verify_domains", "--recheck", stdout=out)
    assert "revoked=0" in out.getvalue()
    assert admit(identity("eva@flaky.example.test")) == "none"


def test_dns_outage_fails_within_the_timeout(orgs, monkeypatch):
    import dns.resolver

    owner, a = orgs["owner_a"], orgs["a"]
    record = add(owner, a, "outage.example.test")
    original = dns.resolver.Resolver

    def unroutable(*args, **kwargs):
        resolver = original(configure=False)
        resolver.nameservers = ["192.0.2.1"]  # TEST-NET-1: never answers.
        return resolver

    monkeypatch.setattr(dns.resolver, "Resolver", unroutable)
    monkeypatch.setattr(domains, "DNS_TIMEOUT_S", 0.5)
    started = time.monotonic()
    result = verify(owner, a, record, None)
    assert time.monotonic() - started < 3
    assert result.status == "pending" and result.last_error_code in {"timeout", "dns_error"}


def test_confirming_an_email_of_the_domain_admits(orgs):
    from allauth.account.models import EmailAddress, EmailConfirmationHMAC

    owner, a = orgs["owner_a"], orgs["a"]
    enable(owner, a)
    dns = FakeDNS()
    record = add(owner, a, "confirm.example.test")
    dns.publish(record)
    verify(owner, a, record, dns)
    person = identity("fabio@confirm.example.test", verified=False)
    address = EmailAddress.objects.get(user=person)
    assert Client().post(reverse("account_confirm_email", args=[EmailConfirmationHMAC(address).key])).status_code == 302
    assert member_roles(owner, a, person) == ["member"]


def test_home_suggests_and_joins_by_domain(orgs):
    owner, a = orgs["owner_a"], orgs["a"]
    person = identity("gabi@suggest.example.test")
    enable(owner, a)
    dns = FakeDNS()
    record = add(owner, a, "suggest.example.test", mode="approval")
    dns.publish(record)
    verify(owner, a, record, dns)
    client = Client()
    assert client.post(reverse("account_login"), {"login": person.email, "password": PASSWORD}).status_code == 302
    home = client.get(reverse("web:home"))
    assert [item["organization_id"] for item in home.context["suggested_organizations"]] == [a.pk]
    assert client.post(reverse("web:join-by-domain", args=[orgs["b"].pk])).status_code == 404
    assert client.post(reverse("web:join-by-domain", args=[a.pk])).status_code == 302
    with request_context(actor=person, request_id=uuid4(), source="test:domain-mine"):
        assert [item.channel for item in admission.my_join_requests(person)] == ["domain"]


def test_platform_revokes_a_disputed_claim(orgs):
    owner, a, owner_b = orgs["owner_a"], orgs["a"], orgs["owner_b"]
    enable(owner, a)
    dns = FakeDNS()
    record = add(owner, a, "dispute.example.test")
    dns.publish(record)
    verify(owner, a, record, dns)
    platform, _ = bootstrap_identity(
        email="domain-platform@example.test", organization="domain-platform", name="P", password=PASSWORD
    )
    client = Client()
    assert client.post(reverse("account_login"), {"login": platform.email, "password": PASSWORD}).status_code == 302
    page = client.get(reverse("platform:domains"))
    [claim] = page.context["rows"]
    stale = client.post(reverse("platform:domain-revoke", args=[claim.pk]), {"revision": claim.revision + 1})
    assert stale.status_code == 409
    assert (
        client.post(reverse("platform:domain-revoke", args=[claim.pk]), {"revision": claim.revision}).status_code == 302
    )
    assert admit(identity("hugo@dispute.example.test")) == "none"
    outsider = Client()
    assert outsider.post(reverse("account_login"), {"login": owner_b.email, "password": PASSWORD}).status_code == 302
    assert outsider.get(reverse("platform:domains")).status_code == 403
