"""Who must enrol MFA (#740): platform rules, the organization's policy and the MFA-only shell.

PostgreSQL with the runtime role. The scenario has two organizations; ``member`` belongs to both,
``admin_a`` administers only A.
"""

import pytest
from django.test import Client
from django.urls import reverse

from skeleton.tenancy.context import system_context
from skeleton.testing.service_api import FIXTURE_PASSWORD, acting_as, build_service_scenario
from tests import support

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]
NAV = 'id="primary-nav"'
ACCOUNT_MENU = 'aria-label="Minha conta"'


@pytest.fixture
def scenario():
    from examples.notes.models import Note

    return build_service_scenario(Note, slug="mfa-policy")


def url(name, organization, **kwargs):
    return reverse(name, kwargs={"organization_id": organization.pk, **kwargs})


def enrol(user):
    from allauth.mfa.models import Authenticator

    with system_context(reason="test:mfa-policy"):
        Authenticator.objects.create(user=user, type=Authenticator.Type.TOTP, data={"secret": "x"})


def withdraw(user):
    from allauth.mfa.models import Authenticator

    with system_context(reason="test:mfa-policy"):
        Authenticator.objects.filter(user=user).delete()


def session(user):
    """A logged-in session; force_login skips the TOTP step of a person who enrolled."""
    client = Client()
    client.force_login(user)
    return client


def set_policy(scenario, policy):
    from skeleton.tenancy.organization_settings import current_settings, revision_of, update_mfa_policy

    enrol(scenario.admin_a)
    with acting_as(scenario.admin_a, scenario.a) as admin:
        update_mfa_policy(actor=admin, policy=policy, expected_revision=revision_of(current_settings()))


def assert_gated(client, path):
    response = client.get(path)
    assert response.status_code == 302 and response["Location"] == reverse("mfa_index"), path


def test_all_members_policy_leaves_only_the_mfa_page_in_that_organization(scenario, subtests):
    set_policy(scenario, "all")
    client = support.login(scenario.member, password=FIXTURE_PASSWORD)
    with subtests.test("every page of A"):
        for path in (
            url("web:dashboard", scenario.a),
            url("web:catalog", scenario.a),
            url("web:organization-settings", scenario.a),
            url("example_notes:home", scenario.a),
        ):
            assert_gated(client, path)
    with subtests.test("the API of A"):
        response = client.get(f"/api/v1/o/{scenario.a.pk}/roles/")
        assert response.status_code == 403 and response.json()["code"] == "mfa_required"
    with subtests.test("the MFA page it sends to has no application menu"):
        page = client.get(reverse("mfa_index"))
        html = page.content.decode()
        assert page.status_code == 200
        assert NAV not in html and ACCOUNT_MENU not in html and "with-sidebar" not in html
        assert reverse("account_logout") in html
    with subtests.test("elsewhere the person moves freely"):
        assert client.get(url("web:dashboard", scenario.b)).status_code == 200
        assert client.get("/profile/").status_code == 200
        assert NAV in client.get(reverse("mfa_index")).content.decode()  # A visit of one's own.
    with subtests.test("back in A, held again"):
        assert_gated(client, url("web:dashboard", scenario.a))
        assert NAV not in client.get(reverse("mfa_index")).content.decode()
    with subtests.test("after the enrolment the organization opens again"):
        enrol(scenario.member)
        assert client.get(url("web:dashboard", scenario.a)).status_code == 200
        assert client.get(url("web:dashboard", scenario.b)).status_code == 200
        assert NAV in client.get(reverse("mfa_index")).content.decode()


def test_administrators_policy_spares_plain_members(scenario):
    set_policy(scenario, "admins")
    member = support.login(scenario.member, password=FIXTURE_PASSWORD)
    assert member.get(url("web:dashboard", scenario.a)).status_code == 200
    withdraw(scenario.admin_a)
    admin = session(scenario.admin_a)
    assert_gated(admin, url("web:dashboard", scenario.a))
    assert_gated(admin, url("web:organization-security", scenario.a))


def test_the_every_person_rule_gates_global_pages_and_every_organization(scenario, settings):
    settings.SKELETON_REQUIRE_MFA = True
    client = support.login(scenario.member, password=FIXTURE_PASSWORD)
    for path in ("/profile/", reverse("web:home"), url("web:dashboard", scenario.a), url("web:dashboard", scenario.b)):
        assert_gated(client, path)
    response = client.get("/api/v1/me/")
    assert response.status_code == 403 and response.json()["code"] == "mfa_required"
    assert client.get(reverse("account_email")).status_code == 200  # Verification stays possible.
    assert ACCOUNT_MENU not in client.get(reverse("account_email")).content.decode()


def test_the_platform_administrator_rule_needs_no_other_setting(scenario, settings):
    settings.SKELETON_REQUIRE_ADMIN_MFA = False
    settings.SKELETON_REQUIRE_PLATFORM_ADMIN_MFA = True
    with system_context(reason="test:platform-admin"):
        scenario.outsider.is_staff = scenario.outsider.is_superuser = True
        scenario.outsider.save(update_fields=["is_staff", "is_superuser"])
    admin = support.login(scenario.outsider, password=FIXTURE_PASSWORD)
    for path in ("/platform/", "/profile/", reverse("platform:users")):
        assert_gated(admin, path)
    member = support.login(scenario.member, password=FIXTURE_PASSWORD)
    assert member.get("/profile/").status_code == 200


def test_the_administrator_sets_the_policy_with_csrf_and_revision(scenario):
    from skeleton.audit.models import AuditEvent
    from skeleton.tenancy.models import OrganizationSettings

    page = url("web:organization-security", scenario.a)
    admin = session(scenario.admin_a)
    # Without TOTP the administrator cannot demand it yet.
    response = admin.post(page, {"mfa_policy": "all", "revision": 0})
    assert response.status_code == 400 and "antes de exigi-la" in response.content.decode()
    enrol(scenario.admin_a)
    admin = session(scenario.admin_a)
    assert admin.get(page).status_code == 200
    response = admin.post(page, {"mfa_policy": "all", "revision": 0})
    assert response.status_code == 302
    assert admin.post(page, {"mfa_policy": "admins", "revision": 0}).status_code == 409
    assert admin.post(page, {"mfa_policy": "", "revision": 1, "display_name": "x"}).status_code == 400
    assert admin.post(page, {"mfa_policy": "nobody", "revision": 1}).status_code == 400
    with system_context(reason="test:mfa-policy-audit", organization_id=scenario.a.pk):
        record = OrganizationSettings.objects.get(organization=scenario.a)
        events = list(AuditEvent.objects.filter(record_sysid=record.pk).values_list("operation", "changes"))
    assert record.mfa_policy == "all" and record.revision == 1
    assert [operation for operation, _ in events] == ["create"]
    assert events[0][1]["mfa_policy"]["new"] == "all"


def test_only_the_administrator_reaches_the_security_page(scenario):
    page = url("web:organization-security", scenario.a)
    member = support.login(scenario.member, password=FIXTURE_PASSWORD)
    assert member.get(page).status_code in (403, 404)
    assert member.post(page, {"mfa_policy": "all", "revision": 0}).status_code in (403, 404)
    other = support.login(scenario.admin_b, password=FIXTURE_PASSWORD)
    assert other.get(page).status_code == 404
    from skeleton.testing.people import add_member

    add_member(organization=scenario.a, user=scenario.denied, role_code="people_manager")
    delegated = support.login(scenario.denied, password=FIXTURE_PASSWORD)
    assert delegated.get(page).status_code in (403, 404)  # A delegated capability is not the administrator.
    enrol(scenario.admin_a)
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(scenario.admin_a)
    assert strict.post(page, {"mfa_policy": "all", "revision": 0}).status_code == 403
    from skeleton.tenancy.models import OrganizationSettings

    with system_context(reason="test:mfa-policy", organization_id=scenario.a.pk):
        assert not OrganizationSettings.objects.filter(organization=scenario.a).exists()


def test_the_settings_area_links_the_security_page(scenario):
    enrol(scenario.admin_a)
    html = session(scenario.admin_a).get(url("web:organization-admin", scenario.a)).content.decode()
    assert f'href="{url("web:organization-security", scenario.a)}"' in html
