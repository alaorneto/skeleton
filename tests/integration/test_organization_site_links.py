"""Links in emails and notifications point to the organization's own address (#487)."""

import re
import uuid

import pytest
from django.core import mail
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from skeleton.notifications.registry import NotificationType, register_notification_type
from skeleton.notifications.services import Recipients, notify
from skeleton.tenancy.context import request_context, tenant_context
from skeleton.tenancy.site import organization_url
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.registries import isolated_notification_types
from skeleton.testing.service_api import FIXTURE_PASSWORD, build_service_scenario
from tests.site_support import disable_site, enable_site, host_of, site_client, sites  # noqa: F401

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]
URL = re.compile(r"https?://[^\s<>\"]+")


@pytest.fixture
def scenario(sites):  # noqa: F811
    from examples.notes.models import Note

    with isolated_notification_types():
        register_notification_type(NotificationType("system.alert", "Alerta", default_channels=("in_app", "email")))
        scenario = build_service_scenario(Note, slug="links")
        enable_site(scenario, scenario.a)
        yield scenario


def alert(scenario, organization, admin):
    mail.outbox.clear()
    with scenario.acting(admin, organization):
        notify("system.alert", recipients=Recipients(users=(scenario.member.pk,)))
    run_pending_jobs()
    [message] = [message for message in mail.outbox if scenario.member.email in message.to]
    return URL.findall(message.body), message


def test_organization_url_rules(scenario):
    a, b = scenario.a.pk, scenario.b.pk
    origin = f"https://{host_of(scenario.a)}"
    with request_context(actor=None, request_id=uuid.uuid4(), source="test:links"):
        assert organization_url(a, f"/o/{a}/tables/x/") == origin + "/tables/x/"
        assert organization_url(a, f"/o/{a}/") == origin + "/"
        assert organization_url(a, "/profile/notifications/") == origin + "/profile/notifications/"
        assert organization_url(a, f"/api/v1/o/{a}/tables/x/records/") == origin + f"/api/v1/o/{a}/tables/x/records/"
        assert organization_url(a, f"/o/{b}/tables/x/") == origin + f"/o/{b}/tables/x/"  # Another sysid: kept.
        assert organization_url(b, f"/o/{b}/tables/x/") is None  # B has no enabled site.


def test_without_base_domain_or_public_url_nothing_changes(scenario, settings):
    a = scenario.a.pk
    with request_context(actor=None, request_id=uuid.uuid4(), source="test:links-off"):
        settings.SKELETON_PUBLIC_URL = ""
        assert organization_url(a, f"/o/{a}/") is None
    with request_context(actor=None, request_id=uuid.uuid4(), source="test:links-off-2"):
        settings.SKELETON_PUBLIC_URL = "https://app.example.test"
        settings.SKELETON_ORGANIZATION_BASE_DOMAIN = ""
        assert organization_url(a, f"/o/{a}/") is None


def test_notification_email_of_an_organization_with_its_address(scenario):
    links, message = alert(scenario, scenario.a, scenario.admin_a)
    origin = f"https://{host_of(scenario.a)}"
    opened = [link for link in links if "/open/" in link]
    assert opened and all(re.fullmatch(origin + r"/notifications/[0-9a-f-]{36}/open/", link) for link in opened)
    assert origin + "/profile/notifications/" in links
    assert message.extra_headers["List-Unsubscribe"] == f"<{origin}/profile/notifications/>"
    # Every link opens the right page on the organization host.
    client = site_client(scenario.member, host_of(scenario.a), FIXTURE_PASSWORD)
    for link in {*opened, origin + "/profile/notifications/"}:
        response = client.get(link[len(origin) :])
        assert response.status_code in (200, 302), link
        assert not getattr(response.wsgi_request, "skeleton_site_refused", False), link


def test_notification_email_of_an_organization_without_address_is_unchanged(scenario):
    links, _ = alert(scenario, scenario.b, scenario.admin_b)
    assert all(link.startswith("https://app.example.test/") for link in links)
    assert any(f"/o/{scenario.b.pk}/notifications/" in link for link in links)
    assert "https://app.example.test/profile/notifications/" in links


def test_turning_the_address_off_sends_new_links_to_the_main_domain(scenario):
    disable_site(scenario, scenario.a, revision=1)
    links, _ = alert(scenario, scenario.a, scenario.admin_a)
    assert all(link.startswith("https://app.example.test/") for link in links)


def _invite(scenario, organization, admin, email, request=None):
    from skeleton.access.models import Role
    from skeleton.tenancy.invitations import issue_invitation

    mail.outbox.clear()
    with tenant_context(organization_id=organization.pk, actor=admin, source="test:links-invite"):
        issue_invitation(user=admin, email=email, role=Role.objects.get(code="member"), request=request)
    return next(message.body for message in mail.outbox if email in message.to)


def test_invitation_links(scenario):
    from django.test import RequestFactory

    main_request = RequestFactory().get("/", HTTP_HOST="app.example.test")
    body = _invite(scenario, scenario.a, scenario.admin_a, "new-a@links.example.test", main_request)
    assert f"https://{host_of(scenario.a)}/invite/{scenario.a.pk}/" in body
    body = _invite(scenario, scenario.b, scenario.admin_b, "new-b@links.example.test", main_request)
    assert f"http://app.example.test/invite/{scenario.b.pk}/" in body  # No address: as before.
    invite_path = URL.search(body.replace("http://app.example.test", "https://app.example.test")).group(0)
    assert invite_path


def test_account_emails_without_organization_are_unchanged(scenario):
    client = Client(enforce_csrf_checks=True)
    client.get(reverse("account_reset_password"))
    mail.outbox.clear()
    client.post(
        reverse("account_reset_password"),
        {"email": scenario.member.email, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
    )
    run_pending_jobs()
    body = next(message.body for message in mail.outbox if scenario.member.email in message.to)
    assert "http://testserver/accounts/password/reset/key/" in body


def test_one_index_read_for_many_links(scenario):
    a = scenario.a.pk
    with request_context(actor=None, request_id=uuid.uuid4(), source="test:links-budget"):
        with CaptureQueriesContext(connection) as queries:
            for number in range(20):
                organization_url(a, f"/o/{a}/notifications/{uuid.UUID(int=number)}/open/")
    reads = [query for query in queries.captured_queries if "sk_tenancy_organizationsite" in query["sql"]]
    assert len(reads) == 1
