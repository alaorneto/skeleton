"""Service accounts: non-interactive members of exactly one organization (#44)."""

import json

import pytest
from allauth.core.exceptions import ImmediateHttpResponse
from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.test import Client, RequestFactory
from django.urls import reverse

from skeleton.accounts.services import create_service_account, deactivate_service_account
from skeleton.tenancy.context import system_context
from tools.security_fixtures import actor_tenant, build_security_fixture

PASSWORD = "Fixture-only-password-2026!"
SERVICE_PASSWORD = "Service-forged-Password-44!"


@pytest.fixture
def access(skeleton_db):
    fixture = build_security_fixture()
    with actor_tenant(fixture.admin, fixture.a):
        service = create_service_account(actor=fixture.admin, name="ERP")
    return fixture, service


def login(user, password=PASSWORD):
    client = Client(enforce_csrf_checks=True)
    client.get(reverse("account_login"))
    response = client.post(
        reverse("account_login"),
        {"login": user.email, "password": password, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
    )
    return client, response


def api(client, method, url, data=None):
    return getattr(client, method)(
        url,
        data=json.dumps(data or {}),
        content_type="application/json",
        HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
    )


def membership_of(fixture, user):
    from skeleton.tenancy.models import Membership

    with actor_tenant(fixture.admin, fixture.a):
        return Membership.objects.get(user=user)


def test_admin_creates_a_service_account_bound_to_one_organization(access):
    from skeleton.access.models import MemberRole
    from skeleton.audit.models import AuditEvent

    fixture, service = access
    assert service.is_service_account and service.service_organization_id == fixture.a.pk
    assert service.email.endswith("@service.invalid") and service.first_name == "ERP"
    assert not service.has_usable_password() and not service.local_password_allowed
    assert not service.is_staff and not service.is_superuser
    assert str(service) == f"{service.email} (conta de serviço)"
    membership = membership_of(fixture, service)
    assert membership.is_active
    with actor_tenant(fixture.admin, fixture.a):
        assert not MemberRole.objects.filter(membership=membership).exists()
    from skeleton.accounts.services import bootstrap_identity
    from skeleton.tenancy.context import request_context

    platform, _ = bootstrap_identity(
        email="service-audit@example.test", organization="service-audit", name="Audit", password=PASSWORD
    )
    # Global events are readable only with the platform GUC on (RLS in PostgreSQL).
    with request_context(actor=platform, request_id=None, source="test:service-audit"):
        created = AuditEvent.objects.get(record_sysid=service.pk, operation="create")
        assert created.actor_sysid == fixture.admin.pk and created.organization_id is None
    client, _ = login(fixture.admin)
    listing = client.get(
        reverse("api:access-list", kwargs={"organization_id": fixture.a.pk, "resource": "memberships"})
    )
    marked = {row["user"]: row["account_type"] for row in listing.json()["results"]}
    assert marked[str(service.pk)] == "service" and marked[str(fixture.alice.pk)] == "human"
    page = client.get(reverse("web:access-list", kwargs={"organization_id": fixture.a.pk, "resource": "memberships"}))
    assert "(conta de serviço)" in page.content.decode()


def test_every_interactive_path_refuses_a_service_account(access):
    from django.contrib.auth import authenticate

    from skeleton.accounts.adapters import AccountAdapter, SocialAccountAdapter
    from skeleton.accounts.models import User

    _, service = access
    # With its real, unusable password the account answers exactly like an unknown email.
    client, response = login(service, SERVICE_PASSWORD)
    _, unknown = login(type("Nobody", (), {"email": "nobody@example.test"})(), SERVICE_PASSWORD)
    assert response.status_code == unknown.status_code == 200
    assert client.get("/api/v1/me/").status_code == 403
    # Even a password forged straight into the database never opens a session.
    with system_context(reason="test:forged-service-password"):
        forged = User.objects.get(pk=service.pk)
        forged.set_password(SERVICE_PASSWORD)
        forged.save(update_fields=["password"])
    assert authenticate(RequestFactory().post("/"), email=service.email, password=SERVICE_PASSWORD) is None
    client, response = login(service, SERVICE_PASSWORD)
    assert response.status_code in {200, 403}
    assert client.get("/api/v1/me/").status_code == 403
    before = len(mail.outbox)
    assert Client().post("/accounts/password/reset/", {"email": service.email}).status_code == 302
    assert len(mail.outbox) == before
    forged_session = Client()
    forged_session.force_login(forged, backend="skeleton.accounts.backends.SkeletonAuthenticationBackend")
    assert forged_session.get("/api/v1/me/").status_code == 403
    request = RequestFactory().get("/")
    with pytest.raises(ImmediateHttpResponse):
        AccountAdapter().pre_login(request, forged)

    class ExistingLogin:
        is_existing = True
        user = forged
        state = {"process": "login"}

    request.user = forged
    with pytest.raises(ImmediateHttpResponse):
        SocialAccountAdapter().pre_social_login(request, ExistingLogin())


def test_roles_with_capabilities_are_refused_directly_and_through_groups(access):
    from skeleton.access.models import AccessGroup, GroupMember, GroupRole, Role

    fixture, service = access
    membership = membership_of(fixture, service)
    client, _ = login(fixture.admin)
    links = reverse("api:access-list", kwargs={"organization_id": fixture.a.pk, "resource": "member-roles"})
    with actor_tenant(fixture.admin, fixture.a):
        roles = {role.code: role for role in Role.objects.all()}
    for code in ("organization_admin", "app_manager", "people_manager"):
        refused = api(client, "post", links, {"membership": str(membership.pk), "role": str(roles[code].pk)})
        assert refused.status_code == 400, code
    member = api(client, "post", links, {"membership": str(membership.pk), "role": str(roles["member"].pk)})
    assert member.status_code == 201
    with actor_tenant(fixture.admin, fixture.a):
        admins = AccessGroup(organization=fixture.a, code="admins", name="Admins")
        admins.save()
        GroupRole(organization=fixture.a, group=admins, role=roles["organization_admin"]).save()
        with pytest.raises(ValidationError), transaction.atomic():
            GroupMember(organization=fixture.a, group=admins, membership=membership).save()
        # The reverse order is refused too: a group holding the service account gains no capability role.
        GroupMember(organization=fixture.a, group=fixture.group, membership=membership).save()
        with pytest.raises(ValidationError), transaction.atomic():
            GroupRole(organization=fixture.a, group=fixture.group, role=roles["app_manager"]).save()


def test_a_second_organization_is_refused(access):
    from skeleton.access.models import Role
    from skeleton.access.services import provision_membership
    from skeleton.tenancy.models import Membership

    fixture, service = access
    client, _ = login(fixture.bob)  # Bob administers B.
    url = reverse("api:access-list", kwargs={"organization_id": fixture.b.pk, "resource": "memberships"})
    assert api(client, "post", url, {"user": str(service.pk)}).status_code == 400
    with system_context(reason="test:service-second-org", organization_id=fixture.b.pk):
        with pytest.raises(ValidationError), transaction.atomic():
            provision_membership(organization=fixture.b, user=service, role=Role.objects.get(code="member"))
        assert not Membership.all_objects.filter(user=service, organization=fixture.b).exists()


def test_deactivation_revokes_access_and_increments_auth_version(access):
    from skeleton.access.models import MemberRole, Role
    from skeleton.accounts.models import User

    fixture, service = access
    membership = membership_of(fixture, service)
    with actor_tenant(fixture.admin, fixture.a):
        MemberRole(organization=fixture.a, membership=membership, role=Role.objects.get(code="member")).save()
        with pytest.raises(ValidationError):
            deactivate_service_account(actor=fixture.admin, user=fixture.alice)  # A human is not a service account.
    with actor_tenant(fixture.bob, fixture.b):
        with pytest.raises(ValidationError):
            deactivate_service_account(actor=fixture.bob, user=service)  # B cannot touch A's account.
    before = User.objects.get(pk=service.pk).auth_version
    with actor_tenant(fixture.admin, fixture.a):
        deactivate_service_account(actor=fixture.admin, user=service)
        assert not MemberRole.objects.filter(membership=membership).exists()
    stored = User.all_objects.get(pk=service.pk)
    assert not stored.is_active and stored.auth_version == before + 1
    assert not membership_of(fixture, service).is_active


def test_people_manager_does_not_manage_service_accounts(access):
    from skeleton.access.models import MemberRole, Role
    from skeleton.access.services import update_membership

    fixture, service = access
    with actor_tenant(fixture.admin, fixture.a):
        MemberRole(
            organization=fixture.a, membership=fixture.alice_membership, role=Role.objects.get(code="people_manager")
        ).save()
    membership = membership_of(fixture, service)
    with actor_tenant(fixture.alice, fixture.a):
        with pytest.raises(PermissionDenied):
            create_service_account(actor=fixture.alice, name="Shadow")
        with pytest.raises(PermissionDenied):
            deactivate_service_account(actor=fixture.alice, user=service)
        with pytest.raises(PermissionDenied):
            update_membership(membership, user=fixture.alice, data={"is_active": False})
        # People management of humans keeps working.
        update_membership(fixture.bob_membership, user=fixture.alice, data={"is_active": False})
    assert membership_of(fixture, service).is_active


def test_service_accounts_stay_outside_platform_lists_and_flags(access):
    from skeleton.accounts.models import User
    from skeleton.accounts.services import bootstrap_identity

    _, service = access
    platform, _ = bootstrap_identity(
        email="service-platform@example.test", organization="service-platform", name="Platform", password=PASSWORD
    )
    client, response = login(platform)
    assert response.status_code == 302
    listing = client.get(reverse("platform:users"), {"q": "service.invalid"})
    assert service.email not in listing.content.decode()
    assert client.get(reverse("platform:user", args=[service.pk])).status_code == 404
    with system_context(reason="test:service-flags"):
        stored = User.objects.get(pk=service.pk)
        stored.is_staff = stored.is_superuser = True
        with pytest.raises((IntegrityError, ValidationError)), transaction.atomic():
            stored.save(update_fields=["is_staff", "is_superuser"])


def test_service_accounts_stay_out_of_the_people_directory_and_inboxes(access):
    from skeleton.notifications.fanout import resolve_people
    from skeleton.service_api.organizations import OrganizationService

    fixture, service = access
    with actor_tenant(fixture.admin, fixture.a):
        people = {person.sysid for person in OrganizationService().members(actor=fixture.admin).items}
        assert fixture.alice.pk in people and service.pk not in people

        class Everyone:
            recipients = {"organization": True}
            actor_id = None

        assert service.pk not in set(resolve_people(Everyone()))
