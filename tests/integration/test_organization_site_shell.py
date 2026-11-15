"""Only the organization on its own address (#483): header, counters, lists and the no-access page."""

import json
import uuid
import zipfile
from io import BytesIO

import pytest
from django.test import Client

from skeleton.notifications.registry import NotificationType, register_notification_type
from skeleton.notifications.services import Recipients, notify
from skeleton.tenancy.context import memberships_for_user, system_context
from skeleton.tenancy.routing import site_scope
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.registries import isolated_notification_types
from skeleton.testing.service_api import FIXTURE_PASSWORD, build_service_scenario
from tests.site_support import enable_site, host_of, site_client, sites  # noqa: F401

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]
NOTE = "example_notes.note"


@pytest.fixture
def scenario(sites):  # noqa: F811
    from examples.notes.models import Note

    with isolated_notification_types():
        register_notification_type(NotificationType("system.notice", "Aviso"))
        scenario = build_service_scenario(Note, slug="shell")
        enable_site(scenario, scenario.a, display_name="Acme Pública")
        enable_site(scenario, scenario.b)
        yield scenario


def notice(scenario, organization, admin, users):
    with scenario.acting(admin, organization):
        notify("system.notice", recipients=Recipients(users=tuple(user.pk for user in users)))
    run_pending_jobs()


def test_header_on_the_organization_host_and_on_the_main_domain(scenario):
    with system_context(reason="test:shell-platform-admin"):
        scenario.member.is_staff = scenario.member.is_superuser = True
        scenario.member.save(update_fields=["is_staff", "is_superuser"])
    notice(scenario, scenario.a, scenario.admin_a, [scenario.member])
    notice(scenario, scenario.b, scenario.admin_b, [scenario.member])
    notice(scenario, scenario.b, scenario.admin_b, [scenario.member])
    site = site_client(scenario.member, host_of(scenario.a), FIXTURE_PASSWORD).get("/").content.decode()
    assert 'id="organization-menu-button"' not in site
    assert "Todas as organizações" not in site and "Suas organizações" not in site
    assert "/platform/" not in site and "Plataforma" not in site
    assert 'href="/notifications/"' in site and 'href="/flows/tasks/"' in site
    assert 'aria-label="1 não lidas"' in site  # Only A's notification.
    platform = site_client(scenario.member, "testserver", FIXTURE_PASSWORD)
    main = platform.get(f"/o/{scenario.a.pk}/").content.decode()
    assert 'id="organization-menu-button"' in main and "/platform/" in main
    assert 'aria-label="3 não lidas"' in main  # The main domain sums every organization, as before.


def test_memberships_are_narrowed_only_for_presentation(scenario):
    from skeleton.tenancy.context import request_context

    both = {scenario.a.pk, scenario.b.pk}
    with request_context(actor=scenario.member, request_id=uuid.uuid4(), source="test:shell-memberships"):
        assert {row["organization_id"] for row in memberships_for_user(scenario.member)} == both
        with site_scope(scenario.a.pk):
            assert [row["organization_id"] for row in memberships_for_user(scenario.member)] == [scenario.a.pk]
            everything = memberships_for_user(scenario.member, all_organizations=True)
            assert {row["organization_id"] for row in everything} == both


def test_api_and_service_list_only_the_site(scenario):
    from skeleton.service_api.organizations import OrganizationService
    from skeleton.tenancy.context import request_context

    client = site_client(scenario.member, host_of(scenario.a), FIXTURE_PASSWORD)
    listed = client.get("/api/v1/organizations/").json()
    rows = listed["results"] if isinstance(listed, dict) else listed
    assert [row["organization_id"] for row in rows] == [str(scenario.a.pk)]
    platform = site_client(scenario.member, "testserver", FIXTURE_PASSWORD).get("/api/v1/organizations/").json()
    assert len(platform["results"] if isinstance(platform, dict) else platform) == 2
    with site_scope(scenario.a.pk):
        with request_context(actor=scenario.member, request_id=uuid.uuid4(), source="test:shell"):
            mine = OrganizationService().my_organizations(actor=scenario.member)
    assert [item.sysid for item in mine] == [scenario.a.pk]


def test_personal_data_export_is_complete_on_any_host(scenario):
    client = site_client(scenario.member, host_of(scenario.a), FIXTURE_PASSWORD)
    response = client.get("/profile/privacy/export/")
    assert response.status_code == 200
    content = b"".join(response.streaming_content) if response.streaming else response.content
    with zipfile.ZipFile(BytesIO(content)) as archive:
        data = json.loads(archive.read("dados.json"))
    assert {item["sysid"] for item in data["organizations"]} == {str(scenario.a.pk), str(scenario.b.pk)}


def _no_access(client, path):
    response = client.get(path)
    assert response.status_code == 404, path
    import re

    # The masked CSRF token of the logout form changes on every response.
    return re.sub(r'name="csrfmiddlewaretoken" value="[^"]+"', "", response.content.decode())


def test_signed_in_without_membership_sees_the_no_access_page(scenario):
    client = site_client(scenario.outsider, host_of(scenario.a), FIXTURE_PASSWORD)
    client.get("/profile/")  # Consumes the "signed in" flash message.
    pages = {path: _no_access(client, path) for path in ("/", f"/tables/{NOTE}/", "/tables/nao-existe/")}
    first = pages["/"]
    assert "Você não tem acesso" in first
    assert f"A conta {scenario.outsider.email} não tem vínculo com Acme Pública." in first
    assert "Pedir acesso" not in first  # A does not accept requests.
    assert len({page for page in pages.values()}) == 1, "the page must not depend on the path"
    api = client.get(f"/api/v1/o/{scenario.a.pk}/tables/{NOTE}/records/")
    assert (api.status_code, api.json()["code"]) == (404, "not_found")


def test_no_access_page_offers_a_request_when_the_organization_accepts_them(scenario):
    from skeleton.tenancy.admission import configure_policy
    from skeleton.testing.service_api import acting_as

    with acting_as(scenario.admin_a, scenario.a) as admin:
        configure_policy(
            actor=admin,
            data={"discoverable": False, "join_requests_enabled": True},
            expected_revision=0,
        )
    client = site_client(scenario.outsider, host_of(scenario.a), FIXTURE_PASSWORD)
    page = client.get("/").content.decode()
    assert "Pedir acesso" in page and f'href="/join/{scenario.a.pk}/"' in page


def test_member_of_b_sees_only_the_public_name_of_a(scenario):
    scenario.create(scenario.a, code="SECRET-A", title="Segredo de A")
    from skeleton.access.models import Role
    from skeleton.access.services import provision_membership
    from skeleton.accounts.services import create_identity

    with system_context(reason="test:shell-only-b"):
        only_b = create_identity(email="only-b@shell.example.test", password=FIXTURE_PASSWORD)
        from allauth.account.models import EmailAddress

        EmailAddress.objects.create(user=only_b, email=only_b.email, verified=True, primary=True)
    with system_context(reason="test:shell-only-b-member", organization_id=scenario.b.pk):
        provision_membership(organization=scenario.b, user=only_b, role=Role.objects.get(code="member"))
    page = site_client(only_b, host_of(scenario.a), FIXTURE_PASSWORD).get(f"/tables/{NOTE}/").content.decode()
    assert "Você não tem acesso" in page and "Acme Pública" in page
    assert "SECRET-A" not in page and "Segredo de A" not in page and "Service contract B" not in page


def test_anonymous_goes_to_the_login_not_to_the_no_access_page(scenario):
    response = Client(HTTP_HOST=host_of(scenario.a)).get("/")
    assert response.status_code == 302 and response["Location"].startswith("/accounts/login/")


def test_inactive_user_cannot_sign_in_on_the_host(scenario):
    with system_context(reason="test:shell-inactive"):
        scenario.outsider.is_active = False
        scenario.outsider.save(update_fields=["is_active"])
    client = Client(HTTP_HOST=host_of(scenario.a))
    client.post("/accounts/login/", {"login": scenario.outsider.email, "password": FIXTURE_PASSWORD})
    response = client.get("/")
    assert response.status_code == 302 and response["Location"].startswith("/accounts/login/")  # Still anonymous.


@pytest.mark.parametrize(
    "path", ["/", "/profile/", "/profile/notifications/", "/profile/privacy/", "/notifications/", "/flows/tasks/"]
)
def test_member_pages_do_not_mention_other_organizations_or_the_platform(scenario, path):
    client = site_client(scenario.member, host_of(scenario.a), FIXTURE_PASSWORD)
    response = client.get(path)
    assert response.status_code == 200, path
    page = response.content.decode().lower()
    for word in ("organizações", "plataforma", "trocar de organização"):
        assert word not in page, (path, word)


def test_the_shell_adds_no_query(scenario, django_assert_max_num_queries):
    """The context processor reuses the memberships query on the organization host."""
    client = site_client(scenario.member, host_of(scenario.a), FIXTURE_PASSWORD)
    platform = site_client(scenario.member, "testserver", FIXTURE_PASSWORD)
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as on_platform:
        platform.get(f"/o/{scenario.a.pk}/")
    with CaptureQueriesContext(connection) as on_site:
        client.get("/")
    # One query more on the site host: the site index read by the host middleware (#482).
    assert len(on_site.captured_queries) <= len(on_platform.captured_queries) + 1
