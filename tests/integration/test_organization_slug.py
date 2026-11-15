"""New organizations take a slug that is a DNS label (#478); older ones keep working."""

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse

from skeleton.accounts.services import bootstrap_identity, create_identity
from skeleton.tenancy.context import system_context
from skeleton.tenancy.models import Organization
from skeleton.tenancy.services import create_organization

pytestmark = pytest.mark.usefixtures("skeleton_db")
PASSWORD = "Mosaic-47!River-Pebble"
FORMAT = "Use de 1 a 63 caracteres"
RESERVED = "Este identificador é reservado"


@pytest.fixture
def owner():
    with system_context(reason="test:slug-owner"):
        return create_identity(email="slug-owner@example.test", password=PASSWORD)


@pytest.mark.parametrize(
    ("slug", "message"), [("Acme", FORMAT), ("a_b", FORMAT), ("-a", FORMAT), ("x" * 64, FORMAT), ("www", RESERVED)]
)
def test_service_refuses_a_slug_that_is_not_a_usable_label(owner, slug, message):
    with pytest.raises(ValidationError) as error:
        create_organization(owner=owner, slug=slug, name="X")
    assert message in error.value.message_dict["slug"][0]
    with system_context(reason="test:slug-check"):
        assert not Organization.all_objects.filter(slug=slug).exists()


def test_service_creates_with_a_valid_label(owner):
    organization = create_organization(owner=owner, slug="acme-1", name="Acme")
    assert organization.slug == "acme-1"


def test_existing_slug_outside_the_rule_keeps_working(owner):
    """No migration and no constraint: an older slug stays valid under /o/<uuid>/."""
    with system_context(reason="test:legacy-slug"):
        legacy = Organization(name="Legacy", slug="Legacy_Org")
        legacy.save()
        assert Organization.objects.get(pk=legacy.pk).slug == "Legacy_Org"


def test_platform_form_refuses_invalid_and_reserved_slugs():
    user, organization = bootstrap_identity(
        email="slug-platform@example.test", organization="slug-platform", name="Slug platform", password=PASSWORD
    )
    client = Client(enforce_csrf_checks=True)
    client.get(reverse("account_login"))
    token = client.cookies["csrftoken"].value
    client.post(reverse("account_login"), {"login": user.email, "password": PASSWORD, "csrfmiddlewaretoken": token})
    token = client.cookies["csrftoken"].value
    url = reverse("platform:organization-create")
    for slug, message in (("WWW", FORMAT), ("a.b", "slug"), ("platform", RESERVED)):
        response = client.post(url, {"name": "X", "slug": slug, "owner": user.email, "csrfmiddlewaretoken": token})
        assert response.status_code == 400, slug
        if message != "slug":
            assert message in response.content.decode()
    response = client.post(url, {"name": "Y", "slug": "valid-one", "owner": user.email, "csrfmiddlewaretoken": token})
    assert response.status_code == 302


def test_platform_detail_shows_the_address(settings):
    settings.SKELETON_ORGANIZATION_BASE_DOMAIN = "example.test"
    settings.SKELETON_PUBLIC_URL = "https://app.example.test"
    user, organization = bootstrap_identity(
        email="slug-detail@example.test", organization="slug-detail", name="Detail", password=PASSWORD
    )
    client = Client()
    client.force_login(user)
    page = client.get(reverse("platform:organization", args=[organization.pk])).content.decode()
    assert "slug-detail.example.test" in page
    with system_context(reason="test:legacy-detail"):
        legacy = Organization(name="Legacy", slug="Legacy_Detail")
        legacy.save()
    page = client.get(reverse("platform:organization", args=[legacy.pk])).content.decode()
    assert "Indisponível: o identificador não pode ser usado como endereço." in page
    settings.SKELETON_ORGANIZATION_BASE_DOMAIN = ""
    page = client.get(reverse("platform:organization", args=[organization.pk])).content.decode()
    assert "Endereço" not in page.split("Identificador", 1)[1].split("UUID", 1)[0]
