"""Platform administration (/platform/): explicit views, no generic admin editors."""

import pytest
from django.apps import apps
from django.contrib import admin
from django.test import Client, override_settings
from django.urls import NoReverseMatch, Resolver404, resolve, reverse

from skeleton.accounts.models import User
from skeleton.accounts.services import bootstrap_identity
from skeleton.tenancy.context import system_context

pytestmark = pytest.mark.usefixtures("skeleton_db")
PASSWORD = "Mosaic-47!River-Pebble"

PLATFORM_PAGES = (
    ("platform:overview", {}),
    ("platform:users", {}),
    ("platform:user-create", {}),
    ("platform:organizations", {}),
    ("platform:organization-create", {}),
    ("platform:catalog", {}),
    ("platform:audit", {}),
    ("platform:apps", {}),
)


def login(user):
    client = Client(enforce_csrf_checks=True)
    assert client.get(reverse("account_login")).status_code == 200
    assert (
        client.post(
            reverse("account_login"),
            {
                "login": user.email,
                "password": PASSWORD,
                "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
            },
        ).status_code
        == 302
    )
    user.refresh_from_db()
    return client


@pytest.fixture
def platform_actor():
    user, organization = bootstrap_identity(
        email="platform-boundary@example.test",
        organization="platform-boundary",
        name="Platform boundary",
        password=PASSWORD,
    )
    return user, organization, login(user)


def test_platform_routes_and_retired_generic_editors(platform_actor):
    user, organization, client = platform_actor
    assert reverse("platform:overview") == "/platform/"
    assert reverse("platform:user", args=[user.pk]) == f"/platform/users/{user.pk}/"
    assert reverse("platform:organization", args=[organization.pk]) == f"/platform/organizations/{organization.pk}/"
    assert admin.site._registry == {}
    for name in ("records_index", "sk_accounts_user_changelist", "sk_tenancy_organization_change", "app_list"):
        with pytest.raises(NoReverseMatch):
            reverse(f"admin:{name}", args=[user.pk] if name.endswith("change") else [])
    for path in (
        "/platform/records/",
        "/platform/records/sk_accounts/user/",
        f"/platform/records/sk_accounts/user/{user.pk}/change/",
        "/admin/sk_accounts/user/",
        "/admin/system/apps/example_notes/",
        "/catalog/",
        "/audit/",
    ):
        assert client.get(path).status_code == 404
    index = client.get(reverse("admin:index"))
    assert index.status_code == 302 and index.url == reverse("platform:overview")


def test_platform_pages_use_site_shell_without_admin_assets(platform_actor):
    _, _, client = platform_actor
    for name, kwargs in PLATFORM_PAGES:
        response = client.get(reverse(name, kwargs=kwargs))
        assert response.status_code == 200, name
        assert b'aria-label="Administra\xc3\xa7\xc3\xa3o da plataforma"' in response.content
        assert b'aria-label="Principal"' in response.content
        assert b"admin/css/" not in response.content


def test_catch_all_does_not_capture_unrelated_root_urls(platform_actor):
    _, organization, client = platform_actor
    assert resolve("/accounts/login/").view_name == "account_login"
    assert resolve("/").view_name == "web:home"
    assert resolve("/api/v1/me/").view_name == "api:me"
    catalog = reverse("web:catalog", kwargs={"organization_id": organization.pk})
    assert resolve(catalog).view_name == "web:catalog"
    assert client.get(catalog).status_code == 200
    viewer = reverse("web:records", kwargs={"organization_id": organization.pk, "table_key": "example_notes.note"})
    assert resolve(viewer).view_name == "web:records"
    if apps.is_installed("examples.notes"):
        assert client.get(viewer).status_code == 200
    for path in ("/sk_accounts/", "/jsi18n/", "/not-a-registered-root/", "/admin/sk_accounts/", "/platform/unknown/"):
        with pytest.raises(Resolver404):
            resolve(path)
        assert client.get(path).status_code == 404


def test_platform_routes_preserve_permission_mfa_csrf_and_cas(platform_actor):
    user, _, client = platform_actor
    overview = reverse("platform:overview")
    change = reverse("platform:user", args=[user.pk])
    anonymous = Client().get(overview)
    assert anonymous.status_code == 302 and anonymous.url.startswith(reverse("account_login"))
    assert client.post(change, {"first_name": "CSRF bypass"}).status_code == 403
    with override_settings(SKELETON_REQUIRE_ADMIN_MFA=True):
        for url in (overview, change, reverse("platform:users"), reverse("admin:index")):
            response = client.get(url)
            assert response.status_code == 302 and response.url == reverse("mfa_index")
    stale_revision = user.revision
    with system_context(reason="test:platform-boundary-cas"):
        user.first_name = "Concurrent winner"
        user.save(update_fields=["first_name"])
    response = client.post(
        change,
        {
            "first_name": "Stale loser",
            "last_name": "",
            "is_active": "on",
            "is_staff": "on",
            "is_superuser": "on",
            "expected_revision": stale_revision,
            "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
        },
    )
    assert response.status_code == 409
    assert User.objects.get(pk=user.pk).first_name == "Concurrent winner"
    # Identity flags are never mass-assigned: unknown fields are ignored by the explicit form.
    current = User.objects.get(pk=user.pk)
    response = client.post(
        change,
        {
            "first_name": "Edited",
            "last_name": "",
            "is_active": "on",
            "is_staff": "on",
            "is_superuser": "on",
            "email": "hijack@example.test",
            "auth_version": "1",
            "expected_revision": current.revision,
            "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
        },
    )
    assert response.status_code == 302
    edited = User.objects.get(pk=user.pk)
    assert edited.first_name == "Edited" and edited.email == user.email
    assert edited.auth_version == current.auth_version
    with system_context(reason="test:platform-boundary-nonplatform"):
        user.refresh_from_db()
        user.is_staff = user.is_superuser = False
        user.save(update_fields=["is_staff", "is_superuser"])
    nonplatform = login(user)
    for url in (overview, change, reverse("platform:catalog"), reverse("admin:index")):
        assert nonplatform.get(url).status_code == 403


def test_platform_creates_identity_and_organization_through_services(platform_actor):
    from allauth.account.models import EmailAddress

    from skeleton.tenancy.models import Organization

    user, _, client = platform_actor
    token = client.cookies["csrftoken"].value
    created = client.post(
        reverse("platform:user-create"),
        {
            "email": "New.Person@Example.test",
            "first_name": "New",
            "last_name": "Person",
            "password1": PASSWORD,
            "password2": PASSWORD,
            "csrfmiddlewaretoken": token,
        },
    )
    assert created.status_code == 302
    person = User.objects.get(email="new.person@example.test")
    assert not person.is_staff and not person.is_superuser
    assert not EmailAddress.objects.filter(user=person).exists()
    duplicate = client.post(
        reverse("platform:user-create"),
        {"email": person.email, "password1": PASSWORD, "password2": PASSWORD, "csrfmiddlewaretoken": token},
    )
    assert duplicate.status_code == 409
    organization = client.post(
        reverse("platform:organization-create"),
        {"name": "Created org", "slug": "created-org", "owner": str(person.pk), "csrfmiddlewaretoken": token},
    )
    assert organization.status_code == 302
    created_org = Organization.objects.get(slug="created-org")
    page = client.get(reverse("platform:organization", args=[created_org.pk]))
    stale = page.context["form"]["expected_revision"].value()
    renamed = client.post(
        reverse("platform:organization", args=[created_org.pk]),
        {
            "name": "Renamed org",
            "is_active": "on",
            "slug": "hijack",
            "expected_revision": stale,
            "csrfmiddlewaretoken": token,
        },
    )
    assert renamed.status_code == 302
    created_org.refresh_from_db()
    assert created_org.name == "Renamed org" and created_org.slug == "created-org"
    conflict = client.post(
        reverse("platform:organization", args=[created_org.pk]),
        {"name": "Stale", "is_active": "on", "expected_revision": stale, "csrfmiddlewaretoken": token},
    )
    assert conflict.status_code == 409
    created_org.refresh_from_db()
    assert created_org.name == "Renamed org"


def test_catalog_edits_only_labels_with_compare_and_swap(platform_actor):
    from skeleton.system.models import FieldDefinition, TableDefinition

    _, _, client = platform_actor
    token = client.cookies["csrftoken"].value
    table = TableDefinition.objects.filter(available=True).order_by("db_table").first()
    original_table = table.db_table
    response = client.post(
        reverse("platform:catalog-table", args=[table.pk]),
        {
            "label": "Rótulo editado",
            "description": "<b>Descrição</b>",
            "db_table": "hijack",
            "expected_revision": table.revision,
            "csrfmiddlewaretoken": token,
        },
    )
    assert response.status_code == 302
    table.refresh_from_db()
    assert table.label == "Rótulo editado" and table.db_table == original_table
    page = client.get(reverse("platform:catalog-table", args=[table.pk]))
    assert b"&lt;b&gt;Descri" in page.content and b"<b>Descri" not in page.content
    stale = client.post(
        reverse("platform:catalog-table", args=[table.pk]),
        {"label": "Stale", "expected_revision": table.revision - 1, "csrfmiddlewaretoken": token},
    )
    assert stale.status_code == 409
    field = FieldDefinition.objects.filter(table=table).first()
    if field is not None:
        updated = client.post(
            reverse("platform:catalog-field", args=[field.pk]),
            {
                "label": "Campo editado",
                "sensitive": "on",
                "expected_revision": field.revision,
                "csrfmiddlewaretoken": token,
            },
        )
        assert updated.status_code == 302
        sensitive = field.sensitive
        field.refresh_from_db()
        assert field.label == "Campo editado" and field.sensitive == sensitive
