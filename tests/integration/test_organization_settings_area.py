"""The settings area of an organization (#621): only links move, no authorization changes.

PostgreSQL with the runtime role. Profiles: plain member, administrator, delegated manager with
only ``members.view``; billing and custom domains on and off; the organization's own host.
"""

import re

import pytest
from django.urls import reverse

from skeleton.testing.people import FIXTURE_PASSWORD, add_member, make_organization, make_person
from tests.support import login

pytestmark = pytest.mark.usefixtures("skeleton_db")
GEAR = 'aria-label="Configuração da organização"'
GROUPS = ["organization", "people", "admission", "applications", "data", "billing"]


@pytest.fixture
def org(monkeypatch):
    from skeleton.access import capabilities
    from skeleton.access.capabilities import MEMBERS_VIEW, BuiltinRole

    # A delegated manager holding only members.view (no builtin role is that narrow).
    monkeypatch.setitem(
        capabilities.BUILTIN_ROLES,
        "people_manager",
        BuiltinRole("people_manager", "Gestor de pessoas", frozenset({MEMBERS_VIEW})),
    )
    admin = make_person(email="admin@settings-area.example.test")
    member = make_person(email="member@settings-area.example.test")
    manager = make_person(email="manager@settings-area.example.test")
    organization = make_organization(owner=admin, slug="settings-area", name="Área de configuração")
    add_member(organization=organization, user=member)
    add_member(organization=organization, user=manager, role_code="people_manager")
    return organization, {"admin": admin, "member": member, "manager": manager}


def url(name, organization, **kwargs):
    return reverse(name, kwargs={"organization_id": organization.pk, **kwargs})


def sidebar(html):
    return html.split('id="org-sidebar"', 1)[1].split("</nav>", 1)[0]


def test_plain_member_sees_only_home_and_apps_without_the_gear(org):
    organization, people = org
    client = login(people["member"], password=FIXTURE_PASSWORD)
    response = client.get(url("web:dashboard", organization))
    assert response.status_code == 200
    menu = sidebar(response.content.decode())
    assert GEAR not in menu and "Início" in menu
    assert "Nenhum aplicativo disponível." in menu
    for name in ("web:catalog", "web:audit", "web:organization-admin"):
        assert f'href="{url(name, organization)}"' not in menu
    assert not response.context["can_open_settings"]
    assert client.get(url("web:organization-admin", organization)).status_code == 404
    home = response.content.decode().split('id="conteudo"', 1)[1]
    assert "Tabelas disponíveis" not in home and "Administração" not in home
    # Authorization is unchanged: Tables stay open by the URL to a member, the admin screens stay closed.
    assert client.get(url("web:catalog", organization)).status_code == 200
    assert client.get(url("web:access-list", organization, resource="roles")).status_code in (403, 404)


def test_administrator_reaches_every_group_through_the_gear(org, settings, subtests):
    organization, people = org
    settings.SKELETON_BILLING_ENABLED = True
    settings.SKELETON_ORGANIZATION_BASE_DOMAIN = "example.test"
    settings.SKELETON_PUBLIC_URL = "https://app.example.test"
    settings.SKELETON_ORGANIZATION_CUSTOM_DOMAINS = True
    client = login(people["admin"], password=FIXTURE_PASSWORD)
    dashboard = client.get(url("web:dashboard", organization)).content.decode()
    hub_url = url("web:organization-admin", organization)
    assert re.search(rf'<a [^>]*href="{re.escape(hub_url)}"[^>]*{GEAR}', sidebar(dashboard))
    hub = client.get(hub_url)
    assert hub.status_code == 200
    assert [group["id"] for group in hub.context["groups"]] == GROUPS
    html = hub.content.decode()
    for name in (
        "web:organization-settings",
        "web:site-domains",
        "web:external",
        "web:join-requests",
        "web:join-links",
        "web:admission",
        "web:verified-domains",
        "web:identity",
        "application-list",
        "integrations:webhooks",
        "integrations:service-accounts",
        "flows:operations",
        "web:catalog",
        "web:audit",
        "billing-overview",
    ):
        with subtests.test(name):
            assert f'href="{url(name, organization)}"' in html
    for resource in ("memberships", "groups", "roles"):
        assert f'href="{url("web:access-list", organization, resource=resource)}"' in html
    with subtests.test("billing and custom domains off"):
        settings.SKELETON_BILLING_ENABLED = False
        settings.SKELETON_ORGANIZATION_CUSTOM_DOMAINS = False
        off = client.get(hub_url)
        assert [group["id"] for group in off.context["groups"]] == GROUPS[:-1]
        assert url("billing-overview", organization) not in off.content.decode()
        assert url("web:site-domains", organization) not in off.content.decode()


def test_delegated_manager_sees_only_people(org):
    organization, people = org
    client = login(people["manager"], password=FIXTURE_PASSWORD)
    dashboard = client.get(url("web:dashboard", organization)).content.decode()
    assert GEAR in sidebar(dashboard)
    hub = client.get(url("web:organization-admin", organization))
    assert hub.status_code == 200
    groups = {group["id"]: [str(item["label"]) for item in group["items"]] for group in hub.context["groups"]}
    assert groups == {"people": ["Pessoas"], "data": ["Tabelas"]}
    menu = sidebar(hub.content.decode())
    assert "Voltar para a organização" in menu
    assert f'href="{url("web:access-list", organization, resource="memberships")}"' in menu
    assert url("web:access-list", organization, resource="roles") not in menu
    assert url("web:audit", organization) not in menu


def test_settings_routes_show_the_settings_menu_and_record_screens_keep_the_functional_one(org, subtests):
    organization, people = org
    client = login(people["admin"], password=FIXTURE_PASSWORD)
    current = {
        url("web:organization-settings", organization): url("web:organization-settings", organization),
        url("web:access-list", organization, resource="roles"): url("web:access-list", organization, resource="roles"),
        url("web:access-list", organization, resource="table-permissions"): url(
            "web:access-list", organization, resource="roles"
        ),
        url("web:access-create", organization, resource="groups"): url(
            "web:access-list", organization, resource="groups"
        ),
        url("web:catalog", organization): url("web:catalog", organization),
        url("web:audit", organization): url("web:audit", organization),
        url("web:join-links", organization): url("web:join-links", organization),
        url("application-list", organization): url("application-list", organization),
        url("integrations:webhooks", organization): url("integrations:webhooks", organization),
        url("flows:drafts", organization): url("flows:operations", organization),
    }
    for page, item in current.items():
        with subtests.test(page):
            response = client.get(page)
            assert response.status_code == 200
            menu = sidebar(response.content.decode())
            assert "Voltar para a organização" in menu and response.context["settings_area"]
            assert re.findall(r'<a class="sidebar-link" href="([^"]+)" aria-current="page"', menu) == [item]
    with subtests.test("dashboard"):
        response = client.get(url("web:dashboard", organization))
        assert not response.context["settings_area"] and "Voltar para a organização" not in sidebar(
            response.content.decode()
        )


def test_area_works_with_clean_urls_on_the_organization_host(org, settings):
    from skeleton.tenancy.organization_settings import update_settings
    from skeleton.testing.service_api import acting_as
    from tests.site_support import BLANK, host_of, site_client, site_settings

    organization, people = org
    site_settings(settings)
    with acting_as(people["admin"], organization) as actor:
        update_settings(actor=actor, data={**BLANK, "site_enabled": True}, expected_revision=0)
    client = site_client(people["admin"], host_of(organization), FIXTURE_PASSWORD)
    hub = client.get("/admin/")
    assert hub.status_code == 200
    html = hub.content.decode()
    assert 'href="/settings/"' in html and 'href="/access/memberships/"' in html and 'href="/audit/"' in html
    assert client.get("/access/roles/").status_code == 200
    member = site_client(people["member"], host_of(organization), FIXTURE_PASSWORD)
    assert member.get("/admin/").status_code == 404
    assert GEAR not in member.get("/").content.decode()


@pytest.mark.needs_example_app
def test_record_screens_keep_the_functional_menu():
    from examples.notes.models import Note
    from skeleton.testing.service_api import build_service_scenario

    scenario = build_service_scenario(Note, slug="settings-records")
    client = login(scenario.member, password=FIXTURE_PASSWORD)
    note = scenario.create(scenario.a, code="n-1", title="Uma nota")
    for page in (
        url("web:records", scenario.a, table_key="example_notes.note"),
        url("web:record", scenario.a, table_key="example_notes.note", sysid=note.pk),
    ):
        response = client.get(page)
        assert response.status_code == 200 and not response.context["settings_area"]
        menu = sidebar(response.content.decode())
        assert "Voltar para a organização" not in menu and "Anotações" in menu
