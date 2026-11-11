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
    for name in ("index", "login", "records_index", "skeleton_releases", "sk_accounts_user_changelist"):
        with pytest.raises(NoReverseMatch):
            reverse(f"admin:{name}")
    for path in (
        "/platform/records/",
        "/platform/records/sk_accounts/user/",
        f"/platform/records/sk_accounts/user/{user.pk}/change/",
        "/admin/sk_accounts/user/",
        "/admin/system/apps/example_notes/",
        "/catalog/",
        "/audit/",
        "/admin/",
        "/admin/login/",
        "/admin/system/releases/",
    ):
        assert client.get(path).status_code == 404
        assert Client().get(path).status_code == 404


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
        for url in (overview, change, reverse("platform:users"), reverse("platform:updates")):
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
    for url in (overview, change, reverse("platform:catalog"), reverse("platform:updates")):
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


# --------------------------------------------------------------------------- organization administrators (#20)


def _person(email):
    from allauth.account.models import EmailAddress

    from skeleton.accounts.services import create_identity

    with system_context(reason="test:platform-person"):
        person = create_identity(email=email, password=PASSWORD)
        EmailAddress.objects.create(user=person, email=email, verified=True, primary=True)
    return person


def _admin_emails(organization):
    from skeleton.access.services import administrator_memberships

    with system_context(reason="test:platform-admins", organization_id=organization.pk):
        return sorted(administrator_memberships().values_list("user__email", flat=True))


def _admin_form(client, organization, *, email, replace=None, version=None):
    page = client.get(reverse("platform:organization", args=[organization.pk]))
    assert page.status_code == 200
    return client.post(
        reverse("platform:organization-administrators", args=[organization.pk]),
        {
            "email": email,
            "replace": str(replace.pk) if replace else "",
            "expected_version": version or page.context["admin_form"]["expected_version"].value(),
            "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
        },
    )


@pytest.fixture
def foreign_organization():
    from skeleton.access.models import Role
    from skeleton.access.services import provision_membership
    from skeleton.accounts.services import create_organization

    owner = _person("owner@designate.example.test")
    organization = create_organization(owner=owner, slug="designate-target", name="Designate target")
    member = _person("plain-member@designate.example.test")
    with system_context(reason="test:designate-member", organization_id=organization.pk):
        provision_membership(organization=organization, user=member, role=Role.objects.get(code="member"))
    return organization, owner, member


def test_system_reasons_open_to_http_are_a_closed_list():
    from uuid import uuid4

    from skeleton.tenancy.context import (
        HTTP_SYSTEM_REASONS,
        PLATFORM_ADMIN_REASONS,
        VALIDATED_IDENTITY_REASONS,
        InvalidContext,
        request_context,
    )

    assert VALIDATED_IDENTITY_REASONS == {"invitation:validated", "sso:validated"}
    assert PLATFORM_ADMIN_REASONS == {
        "organization:create",
        "organization:bootstrap",
        "builtin-roles",
        "organization:designate-admin",
        "platform:organization-inspect",
    }
    assert HTTP_SYSTEM_REASONS == VALIDATED_IDENTITY_REASONS | PLATFORM_ADMIN_REASONS
    platform, _ = bootstrap_identity(
        email="reasons-platform@example.test", organization="reasons", name="Reasons", password=PASSWORD
    )
    member = _person("reasons-member@example.test")
    for reason in sorted(PLATFORM_ADMIN_REASONS):
        with request_context(actor=member, request_id=uuid4(), source="test:reasons"):
            with pytest.raises(InvalidContext), system_context(reason=reason):
                pass
        with request_context(actor=platform, request_id=uuid4(), source="test:reasons"):
            with system_context(reason=reason) as active:
                assert active.system and active.source == "system:" + reason
    with request_context(actor=platform, request_id=uuid4(), source="test:reasons"):
        with pytest.raises(InvalidContext), system_context(reason="job:not-for-http"):
            pass


def test_platform_designates_and_replaces_an_organization_admin(platform_actor, foreign_organization):
    from skeleton.access.models import MemberRole
    from skeleton.audit.models import AuditEvent

    platform, _, client = platform_actor
    organization, owner, member = foreign_organization
    newcomer = _person("newcomer@designate.example.test")
    page = client.get(reverse("platform:organization", args=[organization.pk]))
    assert page.status_code == 200
    overview = page.context["overview"]
    assert overview.active_members == 2
    assert [admin.email for admin in overview.administrators] == [owner.email]
    assert owner.email.encode() in page.content and member.email.encode() not in page.content

    response = _admin_form(client, organization, email="Newcomer@Designate.example.test")
    assert response.status_code == 302, response.content
    assert _admin_emails(organization) == sorted([owner.email, newcomer.email])
    with system_context(reason="test:designate-audit", organization_id=organization.pk):
        link = MemberRole.objects.get(membership__user=newcomer, role__code="organization_admin")
        event = AuditEvent.objects.get(record_sysid=link.pk, operation="create")
        assert event.source == "system:organization:designate-admin"
        assert event.actor_sysid == platform.pk and event.organization_id == organization.pk

    replaced = _admin_form(client, organization, email=member.email, replace=owner)
    assert replaced.status_code == 302
    assert _admin_emails(organization) == sorted([member.email, newcomer.email])
    with system_context(reason="test:designate-audit", organization_id=organization.pk):
        removed = MemberRole.all_objects.get(membership__user=owner, role__code="organization_admin")
        assert removed.deleted_at is not None
        assert AuditEvent.objects.filter(
            record_sysid=removed.pk, operation="soft_delete", source="system:organization:designate-admin"
        ).exists()


def test_designation_refuses_stale_forms_self_and_same_person(platform_actor, foreign_organization):
    platform, _, client = platform_actor
    organization, owner, member = foreign_organization
    page = client.get(reverse("platform:organization", args=[organization.pk]))
    stale = page.context["admin_form"]["expected_version"].value()
    assert _admin_form(client, organization, email=member.email).status_code == 302
    late = _person("late@designate.example.test")
    assert _admin_form(client, organization, email=late.email, version=stale).status_code == 409
    assert _admin_emails(organization) == sorted([owner.email, member.email])
    assert _admin_form(client, organization, email=owner.email, replace=owner).status_code == 400
    assert _admin_form(client, organization, email=platform.email).status_code == 403
    assert _admin_form(client, organization, email="nobody@designate.example.test").status_code == 400
    assert _admin_emails(organization) == sorted([owner.email, member.email])


def test_designation_never_leaves_an_organization_without_admin(platform_actor, foreign_organization, monkeypatch):
    from uuid import uuid4

    from skeleton.access import services as access_services
    from skeleton.access.services import LastAdministrator
    from skeleton.accounts.services import designate_organization_admin
    from skeleton.tenancy.context import request_context

    platform, _, _ = platform_actor
    organization, owner, member = foreign_organization
    # Simulate a grant that does not make the newcomer an administrator (a concurrent revocation).
    monkeypatch.setattr(access_services, "provision_membership", lambda **kwargs: None)
    with request_context(actor=platform, request_id=uuid4(), source="test:last-admin"):
        with pytest.raises(LastAdministrator):
            designate_organization_admin(actor=platform, organization=organization, user=member, replace=owner)
    assert _admin_emails(organization) == [owner.email]


def test_designation_requires_platform_csrf_and_recent_authentication(platform_actor, foreign_organization):
    _, _, client = platform_actor
    organization, owner, member = foreign_organization
    url = reverse("platform:organization-administrators", args=[organization.pk])
    assert client.get(url).status_code == 405
    assert client.post(url, {"email": member.email}).status_code == 403  # CSRF
    session = client.session
    session["account_authentication_methods"][-1]["at"] = 0
    session.save()
    response = _admin_form(client, organization, email=member.email)
    assert response.status_code == 302 and response["Location"].startswith(reverse("account_reauthenticate"))
    assert _admin_emails(organization) == [owner.email]
    outsider = login(member)
    assert outsider.get(reverse("platform:organization", args=[organization.pk])).status_code == 403
    token = outsider.cookies["csrftoken"].value
    assert outsider.post(url, {"email": member.email, "csrfmiddlewaretoken": token}).status_code == 403
    assert _admin_emails(organization) == [owner.email]


def test_platform_admin_does_not_enter_the_organization_it_administers(platform_actor, foreign_organization):
    _, _, client = platform_actor
    organization, _, member = foreign_organization
    assert _admin_form(client, organization, email=member.email).status_code == 302
    for name in ("web:dashboard", "web:catalog"):
        assert client.get(reverse(name, kwargs={"organization_id": organization.pk})).status_code == 404
    assert client.get(f"/api/v1/o/{organization.pk}/memberships/").status_code in {403, 404}
