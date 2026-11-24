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


# --- Changing the slug (#765) -------------------------------------------------------------------

TAKEN = "Este identificador já está em uso"


@pytest.fixture
def change_scenario():
    """Platform administrator (owner of ``a``), the administrator of ``b`` and a plain member of ``b``."""
    from skeleton.testing.people import add_member, make_person

    platform, a = bootstrap_identity(
        email="slug-change-platform@example.test", organization="slug-change-a", name="A", password=PASSWORD
    )
    admin_b, b = bootstrap_identity(
        email="slug-change-b@example.test", organization="slug-change-b", name="B", password=PASSWORD
    )
    with system_context(reason="test:slug-change-demote"):
        admin_b.is_staff = admin_b.is_superuser = False
        admin_b.save(update_fields=["is_staff", "is_superuser"])
        member = make_person(email="slug-change-member@example.test", password=PASSWORD)
    with system_context(reason="test:slug-change-member", organization_id=b.pk):
        add_member(organization=b, user=member)
    return platform, a, admin_b, b, member


@pytest.fixture(autouse=True)
def clean_rate_limit():
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


def fresh(organization):
    with system_context(reason="test:slug-fresh"):
        return Organization.all_objects.get(pk=organization.pk)


def change_own(user, organization, slug, revision):
    from skeleton.tenancy.context import tenant_context
    from skeleton.tenancy.slugs import change_own_slug

    with tenant_context(organization_id=organization.pk, actor=user, source="test:slug-change"):
        return change_own_slug(actor=user, slug=slug, expected_revision=revision)


def change_as_platform(user, organization, slug, revision):
    from uuid import uuid4

    from skeleton.tenancy.context import request_context
    from skeleton.tenancy.services import update_organization

    with request_context(actor=user, request_id=uuid4(), source="test:slug-platform"):
        return update_organization(organization, actor=user, data={"slug": slug}, expected_revision=revision)


def test_slug_availability_states(change_scenario):
    from skeleton.tenancy.slugs import slug_availability

    _platform, a, _admin_b, b, _member = change_scenario
    with system_context(reason="test:slug-deleted"):
        gone = Organization(name="Gone", slug="slug-change-gone")
        gone.save()
        gone.delete()
    cases = {
        "free-label": "available",
        a.slug: "taken",
        "slug-change-gone": "taken",  # Deleted organizations keep their slug.
        "Bad_Slug": "format",
        "": "format",
        "admin": "reserved",
    }
    for slug, expected in cases.items():
        assert slug_availability(slug, organization_id=b.pk) == expected, slug
    assert slug_availability(b.slug, organization_id=b.pk) == "current"
    assert slug_availability(b.slug) == "taken"


def test_platform_changes_the_slug_with_cas_and_audit(change_scenario):
    from skeleton.audit.models import AuditEvent
    from skeleton.common.exceptions import ConcurrentUpdate

    platform, a, _admin_b, b, _member = change_scenario
    changed = change_as_platform(platform, b, "slug-change-new", b.revision)
    assert (changed.slug, changed.revision) == ("slug-change-new", b.revision + 1)
    assert fresh(b).slug == "slug-change-new"
    with pytest.raises(ConcurrentUpdate):
        change_as_platform(platform, b, "slug-change-other", b.revision)
    with pytest.raises(ValidationError) as error:
        change_as_platform(platform, b, a.slug, changed.revision)
    assert TAKEN in error.value.message_dict["slug"][0]
    with pytest.raises(ValidationError):
        change_as_platform(platform, b, "www", changed.revision)
    assert fresh(b).slug == "slug-change-new"
    from uuid import uuid4

    from skeleton.tenancy.context import request_context

    # A global row's audit (no organization) is read by the platform administrator.
    with request_context(actor=platform, request_id=uuid4(), source="test:slug-audit"):
        event = AuditEvent.objects.filter(record_sysid=b.pk, operation="update").order_by("-record_revision").first()
    assert (event.changes["slug"]["old"], event.changes["slug"]["new"]) == ("slug-change-b", "slug-change-new")


def test_organization_admin_changes_own_slug_and_member_cannot(change_scenario):
    from django.core.exceptions import PermissionDenied

    from skeleton.common.exceptions import ConcurrentUpdate

    _platform, a, admin_b, b, member = change_scenario
    with pytest.raises(PermissionDenied):
        change_own(member, b, "slug-change-member", b.revision)
    with pytest.raises(ValidationError) as error:
        change_own(admin_b, b, a.slug, b.revision)
    assert TAKEN in error.value.message_dict["slug"][0]
    assert change_own(admin_b, b, b.slug, b.revision).revision == b.revision  # Unchanged: nothing written.
    changed = change_own(admin_b, b, "slug-change-mine", b.revision)
    assert fresh(changed).slug == "slug-change-mine"
    with pytest.raises(ConcurrentUpdate):
        change_own(admin_b, b, "slug-change-again", b.revision)
    with pytest.raises(PermissionDenied):  # Another organization, where the person is no administrator.
        change_own(admin_b, a, "slug-change-taken-over", a.revision)
    assert fresh(a).slug == "slug-change-a"


def test_a_concurrent_winner_of_the_slug_is_refused_by_the_constraint(change_scenario, monkeypatch):
    """The availability read passed, another transaction took the slug: ``unique`` decides, same message."""
    from skeleton.tenancy import slugs

    platform, a, _admin_b, b, _member = change_scenario
    monkeypatch.setattr(slugs, "slug_availability", lambda *args, **kwargs: slugs.AVAILABLE)
    with pytest.raises(ValidationError) as error:
        change_as_platform(platform, b, a.slug, b.revision)
    assert TAKEN in error.value.message_dict["slug"][0]
    assert fresh(b).slug == "slug-change-b"
    with pytest.raises(ValidationError) as error:
        create_organization(owner=platform, slug=a.slug, name="Copy")
    assert TAKEN in error.value.message_dict["slug"][0]


def test_create_refuses_a_taken_slug_with_a_message(change_scenario):
    platform, a, *_ = change_scenario
    with pytest.raises(ValidationError) as error:
        create_organization(owner=platform, slug=a.slug, name="Copy")
    assert TAKEN in error.value.message_dict["slug"][0]


def test_slug_with_a_bound_identity_provider_cannot_change(change_scenario, monkeypatch):
    from cryptography.fernet import Fernet
    from django.test import override_settings

    from skeleton.accounts import providers
    from skeleton.accounts.identity_providers import create_identity_provider

    platform, a, admin_b, b, _member = change_scenario
    with override_settings(IDENTITY_PROVIDER_ENCRYPTION_KEYS=[Fernet.generate_key().decode()]):
        with system_context(reason="test:slug-idp"):
            create_identity_provider(
                actor=None,
                data={
                    "protocol": "oidc",
                    "provider_id": "slug-change-oidc",
                    "name": "B SSO",
                    "bound_organization": b,
                    "issuer": "https://login.example.test/realms/b",
                    "client_id": "b-client",
                    "secret": "b-secret-value",
                },
            )
        with pytest.raises(ValidationError) as error:
            change_own(admin_b, b, "slug-change-sso", b.revision)
    assert "provedor de identidade" in error.value.message_dict["slug"][0]
    # A provider from the file binds by slug too.
    binding = type("Binding", (), {"organization_slug": a.slug})()
    files = type("Files", (), {"bindings": (binding,)})()
    monkeypatch.setattr(providers, "configured_file_providers", lambda: files)
    with pytest.raises(ValidationError):
        change_as_platform(platform, a, "slug-change-file", a.revision)
    assert (fresh(a).slug, fresh(b).slug) == ("slug-change-a", "slug-change-b")


def test_database_guard_accepts_the_slug_and_still_freezes_the_identity(change_scenario):
    from django.db import DatabaseError, connection, transaction

    _platform, a, *_ = change_scenario
    with system_context(reason="test:slug-guard"):
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE skeleton.sk_tenancy_organization SET slug=%s, revision=revision+1 WHERE sysid=%s",
                ["slug-change-sql", a.pk],
            )
        with pytest.raises(DatabaseError, match="immutable record identity"), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE skeleton.sk_tenancy_organization SET sysid=gen_random_uuid(), revision=revision+1 "
                    "WHERE sysid=%s",
                    [a.pk],
                )
    assert fresh(a).slug == "slug-change-sql"


def test_slug_check_routes(change_scenario, settings):
    platform, a, admin_b, b, member = change_scenario
    platform_client, admin_client, member_client = Client(), Client(), Client()
    platform_client.force_login(platform)
    admin_client.force_login(admin_b)
    member_client.force_login(member)
    signup_url = reverse("web:organization-signup-slug")
    assert admin_client.get(signup_url, {"slug": "x"}).status_code == 404  # The signup is off.
    settings.SKELETON_ORGANIZATION_SIGNUP_ENABLED = True
    assert Client().get(signup_url, {"slug": "x"}).status_code == 302  # Login first.
    answer = member_client.get(signup_url, {"slug": a.slug})
    assert answer.status_code == 200 and answer["Cache-Control"] == "no-store"
    assert answer.json() == {"status": "taken", "message": TAKEN + ". Escolha outro."}
    assert member_client.get(signup_url, {"slug": "x" * 101}).json()["status"] == "format"

    platform_url = reverse("platform:organization-slug")
    assert admin_client.get(platform_url, {"slug": "x"}).status_code == 403
    assert platform_client.get(platform_url, {"slug": "free-one"}).json()["status"] == "available"
    assert platform_client.get(platform_url, {"slug": b.slug, "organization": str(b.pk)}).json()["status"] == "current"
    assert platform_client.get(platform_url, {"slug": b.slug, "organization": "nope"}).json()["status"] == "taken"

    own_url = reverse("web:organization-slug-check", kwargs={"organization_id": b.pk})
    assert admin_client.get(own_url, {"slug": b.slug}).json()["status"] == "current"
    assert admin_client.get(own_url, {"slug": a.slug}).json()["status"] == "taken"
    assert member_client.get(own_url, {"slug": "x"}).status_code in (403, 404)
    other_url = reverse("web:organization-slug-check", kwargs={"organization_id": a.pk})
    assert admin_client.get(other_url, {"slug": "x"}).status_code in (403, 404)
    assert admin_client.post(own_url, {"slug": "x"}).status_code == 405


def test_slug_check_is_rate_limited(change_scenario, monkeypatch):
    from skeleton.web import slug_check

    _platform, _a, admin_b, b, _member = change_scenario
    monkeypatch.setattr(slug_check, "RATE_LIMIT", 2)
    client = Client()
    client.force_login(admin_b)
    url = reverse("web:organization-slug-check", kwargs={"organization_id": b.pk})
    assert [client.get(url, {"slug": "x"}).status_code for _ in range(3)] == [200, 200, 429]


def test_pages_mark_the_input_and_change_the_slug(change_scenario):
    from tests import support

    platform, a, admin_b, b, member = change_scenario
    settings_url = reverse("web:organization-settings", kwargs={"organization_id": b.pk})
    check_url = reverse("web:organization-slug-check", kwargs={"organization_id": b.pk})
    admin = support.login(admin_b, password=PASSWORD)
    page = admin.get(settings_url).content.decode()
    assert f'data-slug-check="{check_url}"' in page and 'data-slug-current="slug-change-b"' in page
    token = admin.cookies["csrftoken"].value
    url = reverse("web:organization-slug", kwargs={"organization_id": b.pk})
    refused = admin.post(url, {"slug": a.slug, "expected_revision": b.revision, "csrfmiddlewaretoken": token})
    assert refused.status_code == 400 and TAKEN in refused.content.decode()
    stale = admin.post(url, {"slug": "slug-change-x", "expected_revision": 999, "csrfmiddlewaretoken": token})
    assert stale.status_code == 409
    saved = admin.post(url, {"slug": "slug-change-page", "expected_revision": b.revision, "csrfmiddlewaretoken": token})
    assert saved.status_code == 302 and saved["Location"] == settings_url
    assert fresh(b).slug == "slug-change-page"
    plain = support.login(member, password=PASSWORD)
    forged = plain.post(
        url,
        {
            "slug": "slug-change-forged",
            "expected_revision": fresh(b).revision,
            "csrfmiddlewaretoken": plain.cookies["csrftoken"].value,
        },
    )
    assert forged.status_code in (403, 404)

    owner = support.login(platform, password=PASSWORD)
    detail = reverse("platform:organization", args=[a.pk])
    page = owner.get(detail).content.decode()
    assert "data-slug-check=" in page and 'data-slug-current="slug-change-a"' in page
    assert "data-slug-check=" in owner.get(reverse("platform:organization-create")).content.decode()
    response = owner.post(
        detail,
        {
            "name": "A",
            "slug": "slug-change-platform",
            "is_active": "on",
            "expected_revision": a.revision,
            "csrfmiddlewaretoken": owner.cookies["csrftoken"].value,
        },
    )
    assert response.status_code == 302
    assert fresh(a).slug == "slug-change-platform"
