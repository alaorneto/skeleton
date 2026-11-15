"""Serving an organization host (#482): clean URLs, redirects and the neutral 404 for everything else."""

import re
import uuid
from datetime import timedelta

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from skeleton.tenancy.context import system_context
from skeleton.testing.service_api import FIXTURE_PASSWORD, build_service_scenario
from tests.site_support import BASE, disable_site, enable_site, host_of, site_client, sites  # noqa: F401

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]
NOTE = "example_notes.note"
LINK = re.compile(r'(?:href|action|src)="([^"]*)"')


@pytest.fixture
def scenario(sites):  # noqa: F811
    from examples.notes.models import Note

    scenario = build_service_scenario(Note, slug="route")
    enable_site(scenario, scenario.a)
    enable_site(scenario, scenario.b)
    return scenario


@pytest.fixture
def member(scenario):
    """The member of both A and B, logged in on A's host."""
    return site_client(scenario.member, host_of(scenario.a), FIXTURE_PASSWORD)


def test_dashboard_on_the_organization_host_has_clean_links(scenario, member):
    page = member.get("/")
    assert page.status_code == 200
    assert page.wsgi_request.resolver_match.view_name == "web:dashboard"
    assert page.wsgi_request.resolver_match.kwargs["organization_id"] == scenario.a.pk
    links = LINK.findall(page.content.decode())
    # The site's own pages are clean; the menu of the other organizations goes away with #483.
    assert links and not [link for link in links if f"/o/{scenario.a.pk}" in link]


@pytest.mark.parametrize(
    ("path", "view"),
    [
        (f"/tables/{NOTE}/", "web:records"),
        ("/notifications/", "notifications:organization"),
        ("/flows/tasks/", "flows:tasks"),
        ("/catalog/", "web:catalog"),
        ("/apps/example_notes/", "example_notes:home"),
    ],
)
def test_clean_paths_serve_the_organization_pages(scenario, member, path, view):
    response = member.get(path)
    assert response.status_code == 200, path
    assert response.wsgi_request.resolver_match.view_name == view
    assert response.wsgi_request.resolver_match.kwargs["organization_id"] == scenario.a.pk


def test_settings_on_the_host_for_the_administrator(scenario):
    admin = site_client(scenario.admin_a, host_of(scenario.a), FIXTURE_PASSWORD)
    response = admin.get("/settings/")
    assert response.status_code == 200 and response.wsgi_request.resolver_match.view_name == "web:organization-settings"


def test_canonical_paths_redirect_to_the_clean_form(scenario, member):
    response = member.get(f"/o/{scenario.a.pk}/tables/{NOTE}/", {"q": "1"})
    assert (response.status_code, response["Location"]) == (302, f"/tables/{NOTE}/?q=1")
    assert member.get(f"/o/{scenario.a.pk}/")["Location"] == "/"
    token = member.cookies["csrftoken"].value
    posted = member.post(f"/o/{scenario.a.pk}/notifications/read-all/", {"csrfmiddlewaretoken": token})
    assert posted.status_code != 302 or posted["Location"] != "/notifications/read-all/"


def test_missing_trailing_slash_is_a_permanent_redirect(scenario, member):
    response = member.get(f"/tables/{NOTE}", {"page": "2"})
    assert (response.status_code, response["Location"]) == (301, f"/tables/{NOTE}/?page=2")


def test_another_organization_is_never_reachable_from_the_host(scenario, member):
    """The member of A and B: B's pages and API are 404 on A's host, with no data of B."""
    b = scenario.b.pk
    for path in (f"/o/{b}/", f"/o/{b}/tables/{NOTE}/", f"/api/v1/o/{b}/tables/{NOTE}/records/"):
        response = member.get(path)
        assert response.status_code == 404, path
        assert response.content == b"Not found."
    # Upper-case UUID of B: still B, still refused.
    assert member.get(f"/o/{str(b).upper()}/tables/{NOTE}/").status_code == 404
    # On the platform host the same person reaches B.
    platform = site_client(scenario.member, "testserver", FIXTURE_PASSWORD)
    assert platform.get(f"/o/{b}/").status_code == 200


def test_member_only_of_b_gets_404_on_a(scenario):
    from skeleton.access.models import Role
    from skeleton.access.services import provision_membership
    from skeleton.accounts.services import create_identity

    with system_context(reason="test:site-only-b"):
        only_b = create_identity(email="only-b@route.example.test", password=FIXTURE_PASSWORD)
        from allauth.account.models import EmailAddress

        EmailAddress.objects.create(user=only_b, email=only_b.email, verified=True, primary=True)
    with system_context(reason="test:site-only-b-member", organization_id=scenario.b.pk):
        provision_membership(organization=scenario.b, user=only_b, role=Role.objects.get(code="member"))
    scenario.create(scenario.a, code="SECRET-A", title="Segredo de A")
    client = site_client(only_b, host_of(scenario.a), FIXTURE_PASSWORD)
    for path in ("/", f"/tables/{NOTE}/"):
        response = client.get(path)
        assert response.status_code == 404
        assert b"SECRET-A" not in response.content and b"Segredo de A" not in response.content


def test_api_of_the_site_works_on_its_host(scenario, member):
    response = member.get(f"/api/v1/o/{scenario.a.pk}/tables/{NOTE}/records/")
    assert response.status_code == 200
    assert member.get("/api/v1/me/").status_code == 200


def test_bearer_token_on_the_organization_host(scenario):
    from skeleton.integrations.tokens.services import issue_personal_token
    from skeleton.tenancy.context import request_context

    with request_context(actor=scenario.member, request_id=uuid.uuid4(), source="test:site-token"):
        issued = issue_personal_token(
            actor=scenario.member,
            name="CLI",
            scopes=[f"{NOTE}:read"],
            expires_at=timezone.now() + timedelta(days=7),
        )
    client = Client(HTTP_HOST=host_of(scenario.a), HTTP_AUTHORIZATION=f"Bearer {issued.text}")
    assert client.get(f"/api/v1/o/{scenario.a.pk}/tables/{NOTE}/records/").status_code == 200
    assert client.get(f"/api/v1/o/{scenario.b.pk}/tables/{NOTE}/records/").status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/platform/",
        "/join/",
        "/join/domain/0b1d8b2e-0000-4000-8000-000000000001/",
        "/billing/webhooks/x/0b1d8b2e-0000-4000-8000-000000000002/",
        "/nope/",
    ],
)
def test_platform_and_global_routes_are_refused(scenario, member, path):
    response = member.get(path)
    assert (response.status_code, response.content) == (404, b"Not found.")


@pytest.mark.parametrize("path", ["/accounts/login/", "/profile/", "/health/live/", "/sw.js"])
def test_identity_and_personal_routes_are_served(scenario, member, path):
    response = member.get(path)
    assert not getattr(response.wsgi_request, "skeleton_site_refused", False), path
    if path != "/sw.js":  # 404 without a VAPID key, but answered by its own view.
        assert response.status_code in (200, 302), path


def test_join_and_invite_of_the_site_only(scenario, member):
    assert not getattr(member.get(f"/join/{scenario.a.pk}/").wsgi_request, "skeleton_site_refused", False)
    assert member.get(f"/join/{scenario.b.pk}/").content == b"Not found."
    assert member.get(f"/invite/{scenario.b.pk}/token/").content == b"Not found."


def test_extra_prefixes_open_a_global_route(scenario, member, settings):
    assert member.get("/accounts-extra/").content == b"Not found."
    settings.SKELETON_ORGANIZATION_SITE_EXTRA_PREFIXES = ["/accounts-extra/"]
    assert not getattr(member.get("/accounts-extra/").wsgi_request, "skeleton_site_refused", False)


def _comparable(response):
    ignored = {"X-Request-ID", "Content-Security-Policy", "Date"}
    return response.status_code, response.content, sorted((k, v) for k, v in response.items() if k not in ignored)


def test_every_refused_host_answers_the_same(scenario):
    """Unknown, disabled, inactive, reserved and two-label hosts: identical answers."""
    from skeleton.tenancy.models import Organization

    revision = 1
    disable_site(scenario, scenario.b, revision=revision)
    with system_context(reason="test:site-refused-inactive"):
        inactive = Organization(name="Inactive", slug="route-inactive")
        inactive.save()
    hosts = [
        f"naoexiste.{BASE}",
        host_of(scenario.b),  # address turned off
        f"{inactive.slug}.{BASE}",  # no site at all
        f"www.{BASE}",  # reserved
        f"a.b.{BASE}",  # two labels
    ]
    answers = {}
    for host in hosts:
        for method in ("get", "post"):
            response = getattr(Client(HTTP_HOST=host), method)("/tables//x")
            assert response.wsgi_request.skeleton_site_refused is True
            answers[(host, method)] = _comparable(response)
    assert len(set(map(repr, answers.values()))) == 1, answers
    status, body, _headers = next(iter(answers.values()))
    assert (status, body) == (404, b"Not found.")


def test_inactive_organization_with_its_site_on(scenario):
    with system_context(reason="test:site-inactive-org"):
        scenario.a.is_active = False
        scenario.a.save(update_fields=["is_active"])
    reference = _comparable(Client(HTTP_HOST=f"naoexiste.{BASE}").get("/"))
    assert _comparable(Client(HTTP_HOST=host_of(scenario.a)).get("/")) == reference


def test_anonymous_is_sent_to_login_with_a_clean_next(scenario):
    response = Client(HTTP_HOST=host_of(scenario.a)).get(f"/tables/{NOTE}/")
    assert response.status_code == 302
    assert response["Location"] == f"/accounts/login/?next=/tables/{NOTE}/"


def test_main_domain_is_unchanged(scenario):
    client = site_client(scenario.member, "testserver", FIXTURE_PASSWORD)
    assert client.get(f"/o/{scenario.a.pk}/").status_code == 200
    assert client.get(f"/tables/{NOTE}/").status_code == 404
    assert client.get("/").wsgi_request.resolver_match.view_name == "web:home"


def test_scope_does_not_leak_to_the_next_request(scenario, member):
    from skeleton.tenancy.routing import site_organization_id

    member.get("/")
    assert site_organization_id() is None
    platform = site_client(scenario.member, "testserver", FIXTURE_PASSWORD)
    page = platform.get(f"/o/{scenario.a.pk}/").content.decode()
    assert f"/o/{scenario.a.pk}/tables/" in page or f"/o/{scenario.a.pk}/catalog/" in page
    assert site_organization_id() is None


def test_scope_is_restored_after_an_exception(scenario, monkeypatch):
    from skeleton.tenancy import site_middleware
    from skeleton.tenancy.routing import site_organization_id

    def boom(request, site):
        assert site_organization_id() == scenario.a.pk
        raise RuntimeError("view failed")

    monkeypatch.setattr(site_middleware, "route_for_site", boom)
    client = Client(HTTP_HOST=host_of(scenario.a), raise_request_exception=False)
    assert client.get("/").status_code == 500
    assert site_organization_id() is None
    assert reverse("web:dashboard", args=[scenario.a.pk]) == f"/o/{scenario.a.pk}/"


def test_refused_requests_have_a_fixed_metric_label(scenario):
    from skeleton.observability.metrics import SITE_REFUSED_ROUTE, route_label

    response = Client(HTTP_HOST=f"naoexiste.{BASE}").get("/")
    assert route_label(response.wsgi_request) == SITE_REFUSED_ROUTE == "organization-site-refused"


def test_equivalence_of_clean_and_canonical_forms(scenario):
    """Every organization page without extra parameters resolves to the same view on both hosts."""
    from tests.unit.test_organization_routing import organization_routes

    site = site_client(scenario.member, host_of(scenario.a), FIXTURE_PASSWORD)
    platform = site_client(scenario.member, "testserver", FIXTURE_PASSWORD)
    checked = 0
    for name, kwargs in organization_routes().items():
        if kwargs:
            continue
        canonical = reverse(name, kwargs={"organization_id": scenario.a.pk})
        clean = canonical[len(f"/o/{scenario.a.pk}") :]
        on_platform = platform.get(canonical)
        on_site = site.get(clean)
        if on_platform.wsgi_request.resolver_match is None:
            continue
        assert on_site.wsgi_request.resolver_match is not None, name
        assert on_site.wsgi_request.resolver_match.view_name == on_platform.wsgi_request.resolver_match.view_name, name
        checked += 1
    assert checked > 15


def test_billing_limits_hold_on_the_organization_host(scenario):
    from skeleton.common.entitlements import NO_OPINION, AccessLevel, override_policy

    class Suspended:
        def entitled_apps(self, organization_id, app_labels):
            return dict.fromkeys(app_labels, True)

        def organization_access(self, organization_id):
            return AccessLevel.SUSPENDED if organization_id == scenario.a.pk else AccessLevel.ACTIVE

        def storage_quota(self, organization_id):
            return NO_OPINION

        def may_use_exempt_routes(self, organization_id, user):
            return False

    client = site_client(scenario.member, host_of(scenario.a), FIXTURE_PASSWORD)
    with override_policy(Suspended()):
        page = client.get("/")
        assert page.status_code == 403 and "suspenso" in page.content.decode()
        api = client.get(f"/api/v1/o/{scenario.a.pk}/tables/{NOTE}/records/")
        assert (api.status_code, api.json()["code"]) == (403, "organization_suspended")
