"""The public page of an organization on its own address (#623), on PostgreSQL with the runtime role."""

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import Client
from django.urls import reverse

from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.testing.service_api import FIXTURE_PASSWORD, acting_as, build_service_scenario
from tests.site_support import PLATFORM_HOST, disable_site, enable_site, host_of, site_client, sites  # noqa: F401
from tests.support import login

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]
PAGE_A = {
    "version": 1,
    "blocks": [
        {"type": "hero", "title": "Acme para você", "subtitle": "Atendimento", "login_button": True},
        {"type": "contact", "email": "contato@acme.test", "phone": "", "address": "", "hours": "9h às 18h"},
        {"type": "text", "heading": "", "body": "<b>negrito</b> não"},
    ],
}
PAGE_B = {"version": 1, "blocks": [{"type": "text", "heading": "", "body": "Conteúdo exclusivo de Beta"}]}


@pytest.fixture
def scenario(sites):  # noqa: F811
    from examples.notes.models import Note

    scenario = build_service_scenario(Note, slug="public-page")
    enable_site(scenario, scenario.a, display_name="Acme Pública", brand_color="#0f766e")
    enable_site(scenario, scenario.b, display_name="Beta Pública")
    return scenario


def save(scenario, organization, document, *, published=True, revision=0, slot="public", user=None):
    from skeleton.tenancy.pages import save_organization_page

    admin = user or (scenario.admin_a if organization.pk == scenario.a.pk else scenario.admin_b)
    with acting_as(admin, organization) as actor:
        return save_organization_page(
            actor=actor, slot=slot, document=document, published=published, expected_revision=revision
        )


def mirror(organization):
    from skeleton.tenancy.models import OrganizationSitePage

    row = OrganizationSitePage.objects.filter(site_organization=organization).first()
    return (row.enabled, row.document) if row is not None else None


def anonymous(organization):
    return Client(HTTP_HOST=host_of(organization))


def test_anonymous_visitor_of_the_own_address_sees_the_published_page(scenario):
    login_redirect = anonymous(scenario.a).get("/")
    assert login_redirect.status_code == 302 and "/accounts/login/" in login_redirect["Location"]
    state = save(scenario, scenario.a, PAGE_A, published=False)
    assert anonymous(scenario.a).get("/").status_code == 302  # A draft is never public.
    save(scenario, scenario.a, PAGE_A, revision=state.revision)
    response = anonymous(scenario.a).get("/")
    html = response.content.decode()
    assert response.status_code == 200
    assert "Acme para você" in html and "Acme Pública" in html and "--color-brand:#0f766e" in html
    assert 'href="mailto:contato@acme.test"' in html and 'href="/accounts/login/"' in html
    assert "&lt;b&gt;negrito&lt;/b&gt;" in html and "<b>negrito" not in html
    assert "no-store" in response["Cache-Control"]


def test_the_main_domain_never_shows_the_page(scenario):
    save(scenario, scenario.a, PAGE_A)
    response = Client(HTTP_HOST=PLATFORM_HOST).get(f"/o/{scenario.a.pk}/")
    assert response.status_code == 302 and "/accounts/login/" in response["Location"]


def test_a_signed_in_member_sees_the_internal_home_never_the_public_page(scenario):
    save(scenario, scenario.a, PAGE_A)
    client = site_client(scenario.member, host_of(scenario.a), FIXTURE_PASSWORD)
    response = client.get("/")
    assert response.status_code == 200 and "Acme para você" not in response.content.decode()
    assert response.context["organization"].sysid == scenario.a.pk


def test_turning_the_address_off_answers_the_neutral_404(scenario):
    settings_a = save(scenario, scenario.a, PAGE_A)
    assert anonymous(scenario.a).get("/").status_code == 200
    from skeleton.tenancy.organization_settings import current_settings

    with acting_as(scenario.admin_a, scenario.a):
        revision = current_settings().revision
    disable_site(scenario, scenario.a, revision=revision)
    response = anonymous(scenario.a).get("/")
    assert response.status_code == 404 and response.content == b"Not found."
    assert mirror(scenario.a) == (False, {})
    assert settings_a.published  # The page itself is kept: turning the address on again shows it.
    with acting_as(scenario.admin_a, scenario.a):
        revision = current_settings().revision
    enable_site(scenario, scenario.a, revision=revision, display_name="Acme Pública")
    assert anonymous(scenario.a).get("/").status_code == 200


def test_the_mirror_follows_publish_unpublish_and_the_address(scenario, subtests):
    with subtests.test("nothing published: no row"):
        assert mirror(scenario.a) is None
    state = save(scenario, scenario.a, PAGE_A)
    with subtests.test("published"):
        enabled, document = mirror(scenario.a)
        assert enabled and [block["type"] for block in document["blocks"]] == ["hero", "contact", "text"]
    state = save(scenario, scenario.a, PAGE_A, published=False, revision=state.revision)
    with subtests.test("unpublished: emptied"):
        assert mirror(scenario.a) == (False, {})
    with subtests.test("a rollback of the caller leaves the mirror as it was"):
        from django.db import transaction

        from skeleton.tenancy.pages import save_organization_page

        with acting_as(scenario.admin_a, scenario.a) as actor:
            with pytest.raises(RuntimeError), transaction.atomic():
                save_organization_page(
                    actor=actor, slot="public", document=PAGE_A, published=True, expected_revision=state.revision
                )
                assert mirror(scenario.a)[0] is True  # Same transaction as the page.
                raise RuntimeError
        assert mirror(scenario.a) == (False, {})


def test_organizations_never_see_each_other_s_page(scenario):
    save(scenario, scenario.b, PAGE_B)
    assert anonymous(scenario.a).get("/").status_code == 302  # A has nothing published.
    save(scenario, scenario.a, PAGE_A)
    html_a = anonymous(scenario.a).get("/").content.decode()
    html_b = anonymous(scenario.b).get("/").content.decode()
    assert "Acme para você" in html_a and "Conteúdo exclusivo de Beta" not in html_a
    assert "Conteúdo exclusivo de Beta" in html_b and "Acme para você" not in html_b
    # Another organization's id is refused on A's host, whatever the page.
    assert anonymous(scenario.a).get(f"/o/{scenario.b.pk}/").status_code == 404


def test_the_mirror_is_read_without_tenant_nor_system_context(scenario):
    from skeleton.common.context import in_context
    from skeleton.tenancy.pages import published_site_page

    save(scenario, scenario.a, PAGE_A)
    assert not in_context()
    assert published_site_page(scenario.a.pk)["blocks"][0]["title"] == "Acme para você"
    assert published_site_page(scenario.b.pk) is None


def test_the_page_table_fails_closed_without_tenant(scenario):
    from skeleton.common.exceptions import MissingTenantContext
    from skeleton.tenancy.context import system_context
    from skeleton.tenancy.models import OrganizationPage

    save(scenario, scenario.a, PAGE_A)
    with pytest.raises(MissingTenantContext), system_context(reason="test:page-no-tenant"):
        list(OrganizationPage.objects.all())
    with connection.cursor() as cursor:  # The runtime role without the tenant GUC sees no row.
        cursor.execute("SELECT count(*) FROM skeleton.sk_tenancy_organizationpage")
        assert cursor.fetchone() == (0,)
    with acting_as(scenario.admin_b, scenario.b):
        assert not OrganizationPage.objects.exists()  # B never reads A's page.


def test_only_the_administrator_edits_with_cas(scenario, subtests, monkeypatch):
    from skeleton.testing.people import add_member, make_person

    manager = make_person(email="manager@public-page.example.test")
    add_member(organization=scenario.a, user=manager, role_code="people_manager")
    editor = reverse("web:organization-page-public", kwargs={"organization_id": scenario.a.pk})
    add = reverse("web:organization-page-public-add", kwargs={"organization_id": scenario.a.pk}) + "?type=text"
    for who, user in (("member", scenario.member), ("delegated manager", manager)):
        with subtests.test(who):
            client = site_client(user, host_of(scenario.a), FIXTURE_PASSWORD)
            assert client.get("/admin/pages/public/").status_code == 404
            token = client.cookies["csrftoken"].value
            denied = client.post(
                "/admin/pages/public/blocks/new/?type=text", {"body": "x", "revision": 0, "csrfmiddlewaretoken": token}
            )
            assert denied.status_code == 404
            with pytest.raises(PermissionDenied):
                save(scenario, scenario.a, PAGE_A, user=user)
    with subtests.test("administrator"):
        admin = site_client(scenario.admin_a, host_of(scenario.a), FIXTURE_PASSWORD)
        page = admin.get("/admin/pages/public/")
        assert page.status_code == 200 and page.context["settings_area"]
        assert 'href="/admin/pages/public/" aria-current="page"' in page.content.decode()
        token = admin.cookies["csrftoken"].value
        created = admin.post(
            "/admin/pages/public/blocks/new/?type=hero",
            {"title": "Olá", "login_button": "on", "revision": 0, "csrfmiddlewaretoken": token},
        )
        assert created.status_code == 302
        stale = admin.post(
            "/admin/pages/public/publish/", {"published": "on", "revision": 0, "csrfmiddlewaretoken": token}
        )
        assert stale.status_code == 409
        assert (
            admin.post(
                "/admin/pages/public/publish/", {"published": "on", "revision": 1, "csrfmiddlewaretoken": token}
            ).status_code
            == 302
        )
        assert anonymous(scenario.a).get("/").status_code == 200
        assert editor == f"/o/{scenario.a.pk}/admin/pages/public/" and add.endswith("?type=text")
    with subtests.test("service CAS and validation"):
        with pytest.raises(ConcurrentUpdate):
            save(scenario, scenario.a, PAGE_A, revision=0)
        with pytest.raises(ValidationError):
            save(scenario, scenario.a, {"version": 1, "blocks": [{"type": "tasks"}]}, revision=2)


def test_the_editor_warns_when_the_address_is_off(scenario):
    from skeleton.tenancy.organization_settings import current_settings

    with acting_as(scenario.admin_a, scenario.a):
        revision = current_settings().revision
    disable_site(scenario, scenario.a, revision=revision)
    admin = login(scenario.admin_a, password=FIXTURE_PASSWORD)
    html = admin.get(
        reverse("web:organization-page-public", kwargs={"organization_id": scenario.a.pk})
    ).content.decode()
    assert "A página só aparece no endereço próprio da organização, que está desligado." in html
    assert reverse("web:organization-settings", kwargs={"organization_id": scenario.a.pk}) in html
