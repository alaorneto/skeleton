"""Custom domains of the organization address, proven by DNS (#488). No network: a fake resolver."""

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse

from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.tenancy import site_domains
from skeleton.tenancy.context import system_context
from skeleton.testing.service_api import FIXTURE_PASSWORD, acting_as, build_service_scenario
from tests import support
from tests.site_support import enable_site, sites  # noqa: F401

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]


class FakeDNS:
    def __init__(self):
        self.records = {}
        self.fail = None

    def __call__(self, name):
        if self.fail:
            raise LookupError(self.fail)
        return self.records.get(name, [])

    def publish(self, record):
        self.records[site_domains.SITE_CHALLENGE_PREFIX + record.domain] = [record.challenge]


@pytest.fixture
def scenario(sites):  # noqa: F811
    from examples.notes.models import Note

    sites.SKELETON_ORGANIZATION_CUSTOM_DOMAINS = True
    scenario = build_service_scenario(Note, slug="customs")
    enable_site(scenario, scenario.a)
    return scenario


def admin_of(scenario, organization):
    return scenario.admin_a if organization.pk == scenario.a.pk else scenario.admin_b


def add(scenario, domain, organization=None):
    organization = organization or scenario.a
    with acting_as(admin_of(scenario, organization), organization) as actor:
        return site_domains.add_site_domain(actor=actor, domain=domain)


def verify(scenario, record, dns, organization=None):
    organization = organization or scenario.a
    with acting_as(admin_of(scenario, organization), organization) as actor:
        return site_domains.verify_site_domain(actor=actor, domain_id=record.pk, resolver=dns)


def claims():
    return {claim.domain: claim.claiming_organization_id for claim in site_domains.site_claims()}


def rows(scenario, organization=None):
    organization = organization or scenario.a
    with acting_as(admin_of(scenario, organization), organization):
        return list(site_domains.site_domains())


def test_register_verify_and_claim(scenario):
    dns = FakeDNS()
    record = add(scenario, "Portal.Cliente.test")
    assert record.domain == "portal.cliente.test" and record.challenge.startswith("skeleton-site=")
    pending = verify(scenario, record, dns)
    assert (pending.status, pending.last_error_code) == ("pending", "challenge_missing")
    assert pending.last_checked_at is not None and claims() == {}
    dns.publish(record)
    verified = verify(scenario, record, dns)
    assert verified.status == "verified" and verified.failure_count == 0
    assert claims() == {"portal.cliente.test": scenario.a.pk}


def test_dns_runs_outside_any_lock(scenario, monkeypatch):
    """The resolver runs before organization_lock: no row lock is held while DNS answers."""
    from skeleton.access import services

    locked = []
    original = services.organization_lock

    def tracking(*args, **kwargs):
        locked.append(True)
        return original(*args, **kwargs)

    calls = []

    def resolver(name):
        calls.append(bool(locked))
        return []

    monkeypatch.setattr(services, "organization_lock", tracking)
    record = add(scenario, "lockless.cliente.test")
    locked.clear()
    verify(scenario, record, resolver)
    assert calls == [False]


def test_domain_of_another_organization_is_a_neutral_failure(scenario):
    dns = FakeDNS()
    enable_site(scenario, scenario.b)
    mine = add(scenario, "shared.cliente.test")
    dns.publish(mine)
    verify(scenario, mine, dns)
    theirs = add(scenario, "shared.cliente.test", scenario.b)
    dns.publish(theirs)
    with pytest.raises(site_domains.DomainUnavailable) as error:
        verify(scenario, theirs, dns, scenario.b)
    assert str(scenario.a.pk) not in str(error.value) and scenario.a.name not in str(error.value)
    assert claims() == {"shared.cliente.test": scenario.a.pk}


@pytest.mark.parametrize(
    "domain",
    [
        "acme.example.test",
        "example.test",
        "app.example.test",
        "testserver",
        "localhost",
        "192.0.2.10",
        "*.cliente.test",
        "cliente.test/x",
        "a@cliente.test",
    ],
)
def test_refused_domains(scenario, domain):
    with pytest.raises(ValidationError) as error:
        add(scenario, domain)
    assert "domain" in error.value.message_dict


def test_platform_host_listed_explicitly_is_refused(scenario, settings):
    settings.ALLOWED_HOSTS = [*settings.ALLOWED_HOSTS, "portal.plataforma.test"]
    with pytest.raises(ValidationError):
        add(scenario, "portal.plataforma.test")


def test_at_most_three_domains_and_no_duplicates(scenario):
    for name in ("one", "two", "three"):
        add(scenario, f"{name}.cliente.test")
    with pytest.raises(ValidationError, match="no máximo 3"):
        add(scenario, "four.cliente.test")
    with pytest.raises(ValidationError, match="já cadastrado"):
        add(scenario, "one.cliente.test")


def test_feature_off_refuses_and_hides_the_page(scenario, settings):
    settings.SKELETON_ORGANIZATION_CUSTOM_DOMAINS = False
    with pytest.raises(ValidationError, match="não aceita domínios próprios"):
        add(scenario, "portal.cliente.test")
    client = support.login(scenario.admin_a, password=FIXTURE_PASSWORD)
    assert client.get(reverse("web:site-domains", args=[scenario.a.pk])).status_code == 404
    settings_page = client.get(reverse("web:organization-settings", args=[scenario.a.pk])).content.decode()
    assert reverse("web:site-domains", args=[scenario.a.pk]) not in settings_page


def test_remove_drops_the_claim_with_cas(scenario):
    dns = FakeDNS()
    record = add(scenario, "gone.cliente.test")
    dns.publish(record)
    record = verify(scenario, record, dns)
    with acting_as(scenario.admin_a, scenario.a) as actor:
        with pytest.raises(ConcurrentUpdate):
            site_domains.revoke_site_domain(actor=actor, domain_id=record.pk, expected_revision=record.revision - 1)
    assert claims() == {"gone.cliente.test": scenario.a.pk}
    with acting_as(scenario.admin_a, scenario.a) as actor:
        site_domains.revoke_site_domain(actor=actor, domain_id=record.pk, expected_revision=record.revision)
    assert claims() == {} and rows(scenario) == []
    from skeleton.tenancy.models import SiteDomain

    with system_context(reason="test:site-domain-tombstone", organization_id=scenario.a.pk):
        assert SiteDomain.objects.with_deleted().get(pk=record.pk).deleted_at is not None


def test_recheck_drops_the_claim_after_three_failures(scenario):
    dns = FakeDNS()
    record = add(scenario, "flaky.cliente.test")
    dns.publish(record)
    verify(scenario, record, dns)
    dns.fail = "timeout"
    for expected in (1, 2):
        report = site_domains.recheck_site_domains(resolver=dns)
        assert (report.failures, report.revoked) == (1, 0)
        assert rows(scenario)[0].failure_count == expected
    assert site_domains.recheck_site_domains(resolver=dns).revoked == 1
    assert rows(scenario)[0].status == "failed" and claims() == {}


def test_recheck_success_resets_the_counter(scenario):
    dns = FakeDNS()
    record = add(scenario, "steady.cliente.test")
    dns.publish(record)
    verify(scenario, record, dns)
    dns.fail = "timeout"
    site_domains.recheck_site_domains(resolver=dns)
    dns.fail = None
    site_domains.recheck_site_domains(resolver=dns)
    assert rows(scenario)[0].failure_count == 0


def test_platform_lists_and_revokes(scenario):
    from skeleton.accounts.services import bootstrap_identity

    dns = FakeDNS()
    record = add(scenario, "dispute.cliente.test")
    dns.publish(record)
    verify(scenario, record, dns)
    claim = site_domains.site_claims().get()
    with pytest.raises(Exception):  # noqa: B017 - any refusal: a member is not the platform
        with acting_as(scenario.admin_a, scenario.a) as actor:
            site_domains.revoke_site_claim(actor=actor, claim_id=claim.pk, expected_revision=claim.revision)
    platform, _ = bootstrap_identity(
        email="platform@customs.example.test", organization="customs-platform", name="P", password=FIXTURE_PASSWORD
    )
    client = support.login(platform, password=FIXTURE_PASSWORD)
    page = client.get(reverse("platform:domains")).content.decode()
    assert "dispute.cliente.test" in page
    url = reverse("platform:site-domain-revoke", args=[claim.pk])
    token = client.cookies["csrftoken"].value
    assert client.post(url, {"revision": claim.revision, "csrfmiddlewaretoken": token}).status_code == 302
    assert claims() == {}
    assert rows(scenario)[0].status == "verified"  # The organization's row is untouched.


def test_a_cannot_read_or_change_b(scenario):
    dns = FakeDNS()
    theirs = add(scenario, "beta.cliente.test", scenario.b)
    assert rows(scenario) == []
    with pytest.raises(site_domains.DomainUnavailable):
        verify(scenario, theirs, dns)
    with acting_as(scenario.admin_a, scenario.a) as actor:
        with pytest.raises(site_domains.DomainUnavailable):
            site_domains.revoke_site_domain(actor=actor, domain_id=theirs.pk, expected_revision=theirs.revision)
    assert len(rows(scenario, scenario.b)) == 1


def test_members_cannot_manage_domains(scenario):
    with acting_as(scenario.member, scenario.a) as actor:
        with pytest.raises(Exception):  # noqa: B017 - organization_lock refuses a non-administrator
            site_domains.add_site_domain(actor=actor, domain="member.cliente.test")
    member = support.login(scenario.member, password=FIXTURE_PASSWORD)
    assert member.get(reverse("web:site-domains", args=[scenario.a.pk])).status_code == 403


def test_page_add_verify_and_instructions(scenario, monkeypatch):
    client = support.login(scenario.admin_a, password=FIXTURE_PASSWORD)
    url = reverse("web:site-domains", args=[scenario.a.pk])
    token = client.cookies["csrftoken"].value
    assert client.post(url, {"domain": "page.cliente.test", "csrfmiddlewaretoken": token}).status_code == 302
    page = client.get(url).content.decode()
    assert "_skeleton-site.page.cliente.test" in page and "skeleton-site=" in page and "app.example.test" in page
    assert "O certificado é emitido pelo operador da instalação." in page
    record = rows(scenario)[0]
    dns = FakeDNS()
    dns.publish(record)
    monkeypatch.setattr(site_domains, "_check", lambda domain, challenge, resolver, prefix: "")
    action = reverse("web:site-domain-action", args=[scenario.a.pk, record.pk])
    assert client.post(action, {"action": "verify", "csrfmiddlewaretoken": token}).status_code == 302
    assert claims() == {"page.cliente.test": scenario.a.pk}
    settings_page = client.get(reverse("web:organization-settings", args=[scenario.a.pk])).content.decode()
    assert url in settings_page
    assert Client().get(url).status_code == 302  # Anonymous: login.


def test_warning_when_the_address_is_off(scenario):
    client = support.login(scenario.admin_b, password=FIXTURE_PASSWORD)
    page = client.get(reverse("web:site-domains", args=[scenario.b.pk])).content.decode()
    assert "O domínio só atende depois que o endereço próprio for ativado." in page


def test_email_domain_admission_is_a_different_proof(scenario):
    """A TXT for the email-domain challenge never proves a site domain."""
    from skeleton.tenancy.domains import CHALLENGE_PREFIX

    dns = FakeDNS()
    record = add(scenario, "mixed.cliente.test")
    dns.records[CHALLENGE_PREFIX + record.domain] = [record.challenge]
    assert verify(scenario, record, dns).status == "pending"
