"""Logo and brand colour of the organization (#168): members only, private headers, AA contrast."""

from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from skeleton.service_api.errors import InvalidInput
from skeleton.service_api.organizations import OrganizationService
from skeleton.tenancy.context import system_context
from skeleton.testing.service_api import FIXTURE_PASSWORD, acting_as, build_service_scenario

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 200
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


@pytest.fixture
def scenario():

    from examples.notes.models import Note

    return build_service_scenario(Note, slug="org-brand")


def login(user):
    client = Client(enforce_csrf_checks=True)
    client.get(reverse("account_login"))
    response = client.post(
        reverse("account_login"),
        {"login": user.email, "password": FIXTURE_PASSWORD, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
    )
    assert response.status_code == 302
    return client


def urls(organization):
    kwargs = {"organization_id": organization.pk}
    return (
        reverse("web:organization-settings", kwargs=kwargs),
        reverse("web:organization-logo-update", kwargs=kwargs),
        reverse("web:organization-logo", kwargs=kwargs),
    )


def upload(client, url, content, name="logo.png", revision=0):
    data = {
        "action": "upload",
        "revision": str(revision),
        "logo": SimpleUploadedFile(name, content),
        "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
    }
    return client.post(url, data)


def settings_row(organization):
    from skeleton.tenancy.models import OrganizationSettings

    with system_context(reason="test:org-brand", organization_id=organization.pk):
        return OrganizationSettings.objects.filter(organization_id=organization.pk).first()


def exists(key):
    from skeleton.files.storage import private_storage

    return private_storage().exists(key)


def test_admin_uploads_and_members_see_the_logo_inline(scenario):
    _, update_url, logo_url = urls(scenario.a)
    admin = login(scenario.admin_a)
    assert upload(admin, update_url, PNG).status_code == 302
    row = settings_row(scenario.a)
    assert row.logo_key.startswith(f"org/{scenario.a.pk}/branding/")
    member = login(scenario.member)
    response = member.get(logo_url)
    assert response.status_code == 200
    assert response["Content-Type"] == "image/png"
    assert response["Content-Disposition"] == "inline"
    assert response["X-Content-Type-Options"] == "nosniff"
    assert "private" in response["Cache-Control"]
    assert response.content == PNG
    etag = response["ETag"]
    assert member.get(logo_url, HTTP_IF_NONE_MATCH=etag).status_code == 304
    with acting_as(scenario.member, scenario.a) as actor:
        assert OrganizationService().settings(actor=actor).has_logo is True


def test_logo_is_closed_to_other_organizations_and_anonymous_visitors(scenario):
    _, update_url, logo_url = urls(scenario.a)
    upload(login(scenario.admin_a), update_url, PNG)
    assert login(scenario.admin_b).get(logo_url).status_code == 404
    assert Client().get(logo_url).status_code == 302  # Login first.
    assert login(scenario.member).post(update_url, {"action": "remove"}).status_code == 403


@pytest.mark.parametrize(
    "content, name",
    [
        (SVG, "logo.svg"),
        (SVG, "logo.png"),  # Text disguised as PNG.
        (PNG, "logo.gif"),  # Name and content disagree.
        (b"GIF89a" + b"\x00" * 100, "logo.gif"),  # GIF is not a logo type.
        (PNG + b"\x00" * (512 * 1024), "logo.png"),  # Above 512 KB.
    ],
)
def test_invalid_logos_are_refused(scenario, content, name):
    _, update_url, _ = urls(scenario.a)
    assert upload(login(scenario.admin_a), update_url, content, name).status_code == 400
    assert settings_row(scenario.a) is None


def test_replace_and_remove_delete_the_previous_object(scenario):
    _, update_url, logo_url = urls(scenario.a)
    admin = login(scenario.admin_a)
    upload(admin, update_url, PNG)
    first = settings_row(scenario.a).logo_key
    assert upload(admin, update_url, PNG, revision=0).status_code == 409  # Stale revision.
    assert upload(admin, update_url, WEBP, "logo.webp", revision=1).status_code == 302
    second = settings_row(scenario.a).logo_key
    assert second != first and exists(second) and not exists(first)
    assert admin.get(logo_url)["Content-Type"] == "image/webp"
    token = admin.cookies["csrftoken"].value
    assert (
        admin.post(update_url, {"action": "remove", "revision": "2", "csrfmiddlewaretoken": token}).status_code == 302
    )
    assert settings_row(scenario.a).logo_key == "" and not exists(second)
    assert admin.get(logo_url).status_code == 404


def test_brand_colour_needs_aa_contrast(scenario):
    service = OrganizationService()
    with acting_as(scenario.admin_a, scenario.a) as admin:
        with pytest.raises(InvalidInput) as error:
            service.update_settings(
                actor=admin,
                display_name="",
                default_language="",
                default_timezone="",
                brand_color="#ffff00",
                expected_revision=0,
            )
        assert "brand_color" in error.value.errors
        service.update_settings(
            actor=admin,
            display_name="Marca A",
            default_language="",
            default_timezone="",
            brand_color="#7c2d12",
            expected_revision=0,
        )


def test_pages_apply_the_brand_of_the_current_organization_only(scenario):
    service = OrganizationService()
    with acting_as(scenario.admin_a, scenario.a) as admin:
        service.update_settings(
            actor=admin,
            display_name="Marca A",
            default_language="",
            default_timezone="",
            brand_color="#7c2d12",
            expected_revision=0,
        )
    member = login(scenario.member)
    page_a = member.get(reverse("web:dashboard", kwargs={"organization_id": scenario.a.pk})).content.decode()
    assert "--color-brand:#7c2d12;" in page_a and "Marca A" in page_a
    page_b = member.get(reverse("web:dashboard", kwargs={"organization_id": scenario.b.pk})).content.decode()
    assert "--color-brand:" not in page_b and "Marca A" not in page_b
    home = member.get(reverse("web:home")).content.decode()
    assert "--color-brand:#7c2d12;" not in home


def test_orphan_cleanup_keeps_the_current_logo_and_removes_stale_objects(scenario, settings):
    from skeleton.files.orphans import orphan_keys
    from skeleton.files.storage import put_stream
    from skeleton.tenancy.branding import logo_key

    _, update_url, _ = urls(scenario.a)
    upload(login(scenario.admin_a), update_url, PNG)
    current = settings_row(scenario.a).logo_key
    stale = logo_key(scenario.a.pk)
    import io

    put_stream(stale, io.BytesIO(PNG), max_bytes=len(PNG))
    later = timezone.now() + timedelta(days=1)
    with system_context(reason="test:org-brand-orphans", organization_id=scenario.a.pk):
        found = orphan_keys(scenario.a.pk, now=later)
    assert stale in found and current not in found
