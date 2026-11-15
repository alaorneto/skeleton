"""The administrator turns the organization's own address on and off (#480); the global index follows."""

import pytest
from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse

from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.tenancy.context import system_context
from skeleton.tenancy.organization_settings import site_refusal, update_settings
from skeleton.tenancy.site import find_site, site_of
from skeleton.testing.service_api import FIXTURE_PASSWORD, acting_as, build_service_scenario
from tests import support

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]
VALUES = {"display_name": "Acme Brasil", "default_language": "", "default_timezone": "", "brand_color": "#1d4ed8"}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture
def sites(settings):
    settings.SKELETON_ORGANIZATION_BASE_DOMAIN = "example.test"
    settings.SKELETON_PUBLIC_URL = "https://app.example.test"
    settings.ALLOWED_HOSTS = [".example.test", "testserver"]
    return settings


@pytest.fixture
def scenario(sites):
    from examples.notes.models import Note

    return build_service_scenario(Note, slug="site")


def write(scenario, revision, organization=None, admin=None, **changes):
    organization = organization or scenario.a
    admin = admin or (scenario.admin_a if organization.pk == scenario.a.pk else scenario.admin_b)
    with acting_as(admin, organization) as actor:
        return update_settings(actor=actor, data={**VALUES, **changes}, expected_revision=revision)


def index_row(organization):
    from skeleton.tenancy.models import OrganizationSite

    return OrganizationSite.objects.filter(site_organization=organization).first()


def test_turn_on_off_and_on_again(scenario):
    a = scenario.a
    assert find_site("site-a") is None and index_row(a) is None
    write(scenario, 0)  # Settings without the address: no index row.
    assert index_row(a) is None
    write(scenario, 1, site_enabled=True)
    # Read with the runtime role and no tenant context: the index is global.
    site = find_site("site-a")
    assert site is not None and site.organization_id == a.pk
    assert (site.name, site.brand_color, site.slug) == ("Acme Brasil", "#1d4ed8", "site-a")
    assert site_of(a.pk) == site
    write(scenario, 2, site_enabled=False)
    assert find_site("site-a") is None and site_of(a.pk) is None
    row = index_row(a)
    assert (row.enabled, row.display_name, row.brand_color, row.logo_key, row.default_language) == (
        False,
        "",
        "",
        "",
        "",
    )
    write(scenario, 3, site_enabled=True)
    assert find_site("site-a").name == "Acme Brasil"
    assert index_row(a).pk == row.pk  # One row per organization, never deleted.


def test_brand_changes_follow_only_while_the_address_is_on(scenario):
    write(scenario, 0, site_enabled=True)
    write(scenario, 1, site_enabled=True, display_name="Novo nome", brand_color="#0f766e")
    assert (find_site("site-a").name, find_site("site-a").brand_color) == ("Novo nome", "#0f766e")
    write(scenario, 2, site_enabled=False)
    revision = index_row(scenario.a).revision
    write(scenario, 3, display_name="Desligado")
    assert index_row(scenario.a).revision == revision  # Off: the index is not written.
    write(scenario, 4, site_enabled=True, display_name="")
    assert find_site("site-a").name == "Service contract A"  # Blank display name: the organization name.


def test_the_logo_follows_the_index(scenario):
    from django.core.files.uploadedfile import SimpleUploadedFile

    from skeleton.tenancy.branding import remove_logo, replace_logo

    write(scenario, 0, site_enabled=True)
    with acting_as(scenario.admin_a, scenario.a) as admin:
        record = replace_logo(actor=admin, upload=SimpleUploadedFile("l.png", PNG), expected_revision=1)
    assert find_site("site-a").logo_key == record.logo_key != ""
    with acting_as(scenario.admin_a, scenario.a) as admin:
        remove_logo(actor=admin, expected_revision=record.revision)
    assert find_site("site-a").logo_key == ""


def test_failure_after_save_leaves_no_index_row(scenario):
    with pytest.raises(RuntimeError):
        with transaction.atomic():
            write(scenario, 0, site_enabled=True)
            raise RuntimeError("after the save")
    assert index_row(scenario.a) is None and find_site("site-a") is None


def test_stale_revision_changes_nothing(scenario):
    write(scenario, 0)
    with pytest.raises(ConcurrentUpdate):
        write(scenario, 0, site_enabled=True)
    assert index_row(scenario.a) is None


def test_refused_without_base_domain_or_with_an_unusable_slug(scenario, settings):
    from skeleton.tenancy.models import Organization

    settings.SKELETON_ORGANIZATION_BASE_DOMAIN = ""
    with pytest.raises(ValidationError) as error:
        write(scenario, 0, site_enabled=True)
    assert "esta instalação não oferece endereço próprio" in error.value.message_dict["site_enabled"][0]
    assert index_row(scenario.a) is None
    settings.SKELETON_ORGANIZATION_BASE_DOMAIN = "example.test"
    assert "identificador" in site_refusal(Organization(name="Legacy", slug="Acme"))
    assert site_refusal(scenario.a) is None


def test_inactive_or_deleted_organization_has_no_site(scenario):
    write(scenario, 0, site_enabled=True)
    with system_context(reason="test:site-inactive"):
        scenario.a.is_active = False
        scenario.a.save(update_fields=["is_active"])
    assert find_site("site-a") is None and site_of(scenario.a.pk) is None
    assert index_row(scenario.a).enabled is True


def test_site_enabled_is_audited(scenario):
    from skeleton.audit.models import AuditEvent
    from skeleton.tenancy.models import OrganizationSettings

    write(scenario, 0)
    write(scenario, 1, site_enabled=True)
    with system_context(reason="test:site-audit", organization_id=scenario.a.pk):
        record = OrganizationSettings.objects.get()
        changes = AuditEvent.objects.filter(record_sysid=record.pk, record_revision=2).values_list("changes", flat=True)
        assert (changes[0]["site_enabled"]["old"], changes[0]["site_enabled"]["new"]) == (False, True)


def test_organization_a_does_not_touch_b(scenario):
    write(scenario, 0, site_enabled=True)
    assert index_row(scenario.b) is None and find_site("site-b") is None
    write(scenario, 0, organization=scenario.b, site_enabled=True, display_name="B")
    write(scenario, 1, site_enabled=False)
    assert find_site("site-b").name == "B" and find_site("site-a") is None


def _login(user):
    return support.login(user, password=FIXTURE_PASSWORD)


def test_settings_page_section_and_post(scenario, settings):
    url = reverse("web:organization-settings", kwargs={"organization_id": scenario.a.pk})
    admin = _login(scenario.admin_a)
    page = admin.get(url).content.decode()
    assert "https://site-a.example.test" in page and 'name="site_enabled"' in page
    assert "ficam visíveis para qualquer pessoa" in page
    token = admin.cookies["csrftoken"].value
    data = {**VALUES, "business_hours": "", "holidays": "", "revision": "0", "csrfmiddlewaretoken": token}
    assert admin.post(url, {**data, "site_enabled": "on"}).status_code == 302
    assert find_site("site-a") is not None
    assert admin.post(url, {**data, "revision": "1"}).status_code == 302
    assert find_site("site-a") is None
    settings.SKELETON_ORGANIZATION_BASE_DOMAIN = ""
    page = admin.get(url).content.decode()
    assert "Esta instalação não oferece endereço próprio para organizações." in page
    assert 'name="site_enabled"' not in page
    response = admin.post(url, {**data, "revision": "2", "site_enabled": "on"})
    assert response.status_code == 400 and find_site("site-a") is None


def test_unusable_slug_text_on_the_page(scenario, monkeypatch):
    import skeleton.tenancy.organization_settings as module

    monkeypatch.setattr(module, "site_refusal", lambda organization: "x")
    page = _login(scenario.admin_a).get(reverse("web:organization-settings", args=[scenario.a.pk])).content.decode()
    assert "O identificador desta organização não pode ser usado como endereço." in page
    assert 'name="site_enabled"' not in page


def test_members_and_other_organizations_cannot_turn_it_on(scenario):
    url = reverse("web:organization-settings", kwargs={"organization_id": scenario.a.pk})
    member = _login(scenario.member)
    assert 'name="site_enabled"' not in member.get(url).content.decode()
    token = member.cookies["csrftoken"].value
    data = {**VALUES, "revision": "0", "site_enabled": "on", "csrfmiddlewaretoken": token}
    assert member.post(url, data).status_code == 403
    other = _login(scenario.admin_b)
    data["csrfmiddlewaretoken"] = other.cookies["csrftoken"].value
    assert other.post(url, data).status_code == 404
    assert index_row(scenario.a) is None
