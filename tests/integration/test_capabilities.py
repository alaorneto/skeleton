"""Delegated capabilities: builtin people/app managers without full administration (#17)."""

import json

import pytest
from django.test import override_settings
from django.urls import reverse

from skeleton.access.capabilities import (
    BUILTIN_ROLES,
    CAPABILITIES,
    capabilities_for,
    has_capability,
    role_capabilities,
)
from skeleton.accounts.services import bootstrap_identity, create_identity
from skeleton.tenancy.context import system_context, tenant_context
from tests import support

pytestmark = pytest.mark.usefixtures("skeleton_db")
PASSWORD = "Capability-matrix-Password!83"
RESOURCES = (
    "memberships",
    "invitations",
    "roles",
    "groups",
    "member-roles",
    "group-members",
    "group-roles",
    "table-permissions",
    "record-grants",
)


def test_registry_declares_capabilities_only_for_builtin_rows():
    assert BUILTIN_ROLES["organization_admin"].capabilities == CAPABILITIES
    assert BUILTIN_ROLES["member"].capabilities == frozenset()
    assert BUILTIN_ROLES["app_manager"].capabilities == {"apps.manage"}
    assert {"members.view", "members.manage", "invitations.manage", "admission.decide"} == set(
        BUILTIN_ROLES["people_manager"].capabilities
    )
    # A custom role that reuses a builtin code never carries the capability.
    assert role_capabilities("organization_admin", is_builtin=False) == frozenset()
    assert role_capabilities("unknown", is_builtin=True) == frozenset()
    with pytest.raises(ValueError):
        has_capability(None, "not.a.capability")


def login(user):
    return support.login(user, password=PASSWORD)


def send(client, method, url, data=None, revision=None):
    headers = {"HTTP_X_CSRFTOKEN": client.cookies["csrftoken"].value}
    if revision is not None:
        headers["HTTP_IF_MATCH"] = revision
    return getattr(client, method)(url, data=json.dumps(data or {}), content_type="application/json", **headers)


def member_with_role(owner, organization, email, code, *, via_group=False):
    from allauth.account.models import EmailAddress

    from skeleton.access.models import AccessGroup, GroupMember, GroupRole, MemberRole, Role
    from skeleton.tenancy.models import Membership

    with system_context(reason="test:capability-identity"):
        user = create_identity(email=email, password=PASSWORD)
        EmailAddress.objects.create(user=user, email=email, verified=True, primary=True)
    with tenant_context(organization_id=organization.pk, actor=owner, source="test:capability-role"):
        membership = Membership.objects.create(organization=organization, user=user)
        role = Role.objects.get(code=code, is_builtin=True)
        if via_group:
            group = AccessGroup.objects.create(organization=organization, code=f"g-{code}", name=f"Group {code}")
            GroupMember.objects.create(organization=organization, group=group, membership=membership)
            GroupRole.objects.create(organization=organization, group=group, role=role)
        else:
            MemberRole.objects.create(organization=organization, membership=membership, role=role)
    return user, membership


@pytest.fixture
def org():
    owner, organization = bootstrap_identity(
        email="capability-owner@example.test", organization="capability-a", name="Capability A", password=PASSWORD
    )
    with system_context(reason="test:capability-owner-not-platform"):
        owner.is_staff = owner.is_superuser = False
        owner.save(update_fields=["is_staff", "is_superuser"])
    return owner, organization


def test_new_organization_has_every_builtin_role(org):
    from skeleton.access.models import Role

    owner, organization = org
    with tenant_context(organization_id=organization.pk, actor=owner, source="test:capability-roles"):
        assert set(Role.objects.filter(is_builtin=True).values_list("code", flat=True)) == set(BUILTIN_ROLES)
        assert capabilities_for(owner) == CAPABILITIES


def test_capability_direct_and_by_group_is_tenant_bound_and_revoked_immediately(org):
    from skeleton.access.models import MemberRole

    owner, organization = org
    direct, membership = member_with_role(owner, organization, "pm-direct@example.test", "people_manager")
    grouped, _ = member_with_role(owner, organization, "am-group@example.test", "app_manager", via_group=True)
    with tenant_context(organization_id=organization.pk, actor=direct, source="test:capability-direct"):
        assert has_capability(direct, "members.manage")
        assert not has_capability(direct, "access.manage")
    with tenant_context(organization_id=organization.pk, actor=grouped, source="test:capability-group"):
        assert capabilities_for(grouped) == {"apps.manage"}
    # The same person in another organization holds nothing there.
    _, other = bootstrap_identity(
        email="capability-b@example.test", organization="capability-b", name="Capability B", password=PASSWORD
    )
    from skeleton.access.models import Role
    from skeleton.tenancy.models import Membership

    with system_context(reason="test:capability-b", organization_id=other.pk):
        Membership.objects.create(organization=other, user=direct)
        MemberRole.objects.create(
            organization=other,
            membership=Membership.objects.get(user=direct),
            role=Role.objects.get(code="member"),
        )
    with tenant_context(organization_id=other.pk, actor=direct, source="test:capability-other"):
        assert capabilities_for(direct) == frozenset()
    with tenant_context(organization_id=organization.pk, actor=owner, source="test:capability-revoke"):
        MemberRole.objects.get(membership=membership, role__code="people_manager").delete()
    with tenant_context(organization_id=organization.pk, actor=direct, source="test:capability-after"):
        assert capabilities_for(direct) == frozenset()


def test_role_by_resource_matrix_in_html_and_api(org):
    owner, organization = org
    people, _ = member_with_role(owner, organization, "pm@example.test", "people_manager")
    apps_user, _ = member_with_role(owner, organization, "am@example.test", "app_manager")
    plain, _ = member_with_role(owner, organization, "plain@example.test", "member")
    allowed = {
        "people_manager": {"memberships", "invitations"},
        "app_manager": set(),
        "organization_admin": set(RESOURCES),
    }
    actors = {"people_manager": people, "app_manager": apps_user, "organization_admin": owner}
    for code, user in actors.items():
        client = login(user)
        for resource in RESOURCES:
            kwargs = {"organization_id": organization.pk, "resource": resource}
            html = client.get(reverse("web:access-list", kwargs=kwargs)).status_code
            api = client.get(reverse("api:access-list", kwargs=kwargs)).status_code
            expected = 200 if resource in allowed[code] else 404
            assert (code, resource, html, api) == (code, resource, expected, expected)
        apps_html = client.get(reverse("application-list", kwargs={"organization_id": organization.pk})).status_code
        assert apps_html == (200 if code in {"organization_admin", "app_manager"} else 404)
    # Someone without any capability keeps the historical 403 of admin-only pages.
    client = login(plain)
    kwargs = {"organization_id": organization.pk, "resource": "memberships"}
    assert client.get(reverse("web:access-list", kwargs=kwargs)).status_code == 403
    assert client.get(reverse("api:access-list", kwargs=kwargs)).status_code == 403


def test_people_manager_cannot_escalate(org):
    from skeleton.access.models import Role
    from skeleton.tenancy.models import Invitation, Membership

    owner, organization = org
    people, _ = member_with_role(owner, organization, "pm-escalate@example.test", "people_manager")
    admin_two, admin_membership = member_with_role(owner, organization, "admin-two@example.test", "organization_admin")
    plain, plain_membership = member_with_role(owner, organization, "plain-two@example.test", "member")
    with tenant_context(organization_id=organization.pk, actor=owner, source="test:escalate-roles"):
        roles = {role.code: role for role in Role.objects.filter(is_builtin=True)}
        admin_membership = Membership.objects.get(pk=admin_membership.pk)
        plain_membership = Membership.objects.get(pk=plain_membership.pk)
    client = login(people)
    invitations = reverse("api:access-list", kwargs={"organization_id": organization.pk, "resource": "invitations"})
    for code in ("organization_admin", "app_manager", "people_manager"):
        response = send(client, "post", invitations, {"email": f"{code}@example.test", "role": str(roles[code].pk)})
        assert response.status_code == 403, (code, response.content)
        assert response.json()["code"] == "privileged_role"
    from uuid import uuid4

    unknown = send(client, "post", invitations, {"email": "unknown-role@example.test", "role": str(uuid4())})
    assert unknown.status_code == 400  # A role that is not this organization's stays an unavailable reference.
    response = send(client, "post", invitations, {"email": "new-member@example.test", "role": str(roles["member"].pk)})
    assert response.status_code == 201, response.content
    with tenant_context(organization_id=organization.pk, actor=owner, source="test:escalate-check"):
        assert list(Invitation.objects.values_list("email", flat=True)) == ["new-member@example.test"]
    detail = reverse(
        "api:access-detail",
        kwargs={"organization_id": organization.pk, "resource": "memberships", "sysid": admin_membership.pk},
    )
    revision = f'"{admin_membership.revision}"'
    assert send(client, "patch", detail, {"is_active": False}, revision).status_code == 403
    assert send(client, "delete", detail, None, revision).status_code == 403
    plain_detail = reverse(
        "api:access-detail",
        kwargs={"organization_id": organization.pk, "resource": "memberships", "sysid": plain_membership.pk},
    )
    response = send(client, "patch", plain_detail, {"is_active": False}, f'"{plain_membership.revision}"')
    assert response.status_code == 200, response.content
    with tenant_context(organization_id=organization.pk, actor=owner, source="test:escalate-after"):
        assert Membership.objects.get(pk=admin_membership.pk).is_active
        assert not Membership.objects.get(pk=plain_membership.pk).is_active
    # The role choices of an invitation offer only roles without capabilities.
    from skeleton.web.references import target_queryset

    field = Invitation._meta.get_field("role")
    with tenant_context(organization_id=organization.pk, actor=people, source="test:escalate-picker"):
        codes = set(target_queryset(field, user=people, source_model=Invitation).values_list("code", flat=True))
    assert codes == {"member"}
    with tenant_context(organization_id=organization.pk, actor=owner, source="test:escalate-picker-admin"):
        codes = set(target_queryset(field, user=owner, source_model=Invitation).values_list("code", flat=True))
    assert codes == set(BUILTIN_ROLES)
    assert admin_two.is_active


def test_reconciliation_creates_missing_builtins_once_and_reports_collisions(org):
    from skeleton.access.models import Role
    from skeleton.access.services import reconcile_builtin_roles
    from skeleton.tenancy.models import Organization

    owner, _ = org
    with system_context(reason="test:legacy-organization"):
        legacy = Organization(name="Legacy", slug="legacy")
        legacy.save()
    with system_context(reason="test:legacy-roles", organization_id=legacy.pk):
        Role(organization=legacy, code="organization_admin", name="Administrador", is_builtin=True).save()
        Role(organization=legacy, code="member", name="Membro", is_builtin=True).save()
        Role(organization=legacy, code="app_manager", name="Custom apps", is_builtin=False).save()
    assert reconcile_builtin_roles(organization_id=legacy.pk) == ["app_manager"]
    assert reconcile_builtin_roles(organization_id=legacy.pk) == ["app_manager"]
    with system_context(reason="test:legacy-check", organization_id=legacy.pk):
        codes = list(Role.objects.order_by("code").values_list("code", "is_builtin"))
    assert codes == [
        ("app_manager", False),
        ("member", True),
        ("organization_admin", True),
        ("people_manager", True),
    ]


def test_reconcile_command_reports_collision(org):
    from django.core.management import CommandError, call_command

    from skeleton.access.models import Role
    from skeleton.tenancy.models import Organization

    with system_context(reason="test:command-organization"):
        legacy = Organization(name="Legacy command", slug="legacy-command")
        legacy.save()
    with system_context(reason="test:command-roles", organization_id=legacy.pk):
        Role(organization=legacy, code="people_manager", name="Custom people", is_builtin=False).save()
    with pytest.raises(CommandError):
        call_command("reconcile_builtin_roles")


@override_settings(SKELETON_REQUIRE_ADMIN_MFA=True)
def test_delegated_manager_needs_mfa(org):
    owner, organization = org
    people, _ = member_with_role(owner, organization, "pm-mfa@example.test", "people_manager")
    plain, _ = member_with_role(owner, organization, "plain-mfa@example.test", "member")
    client = login(people)
    dashboard = reverse("web:dashboard", kwargs={"organization_id": organization.pk})
    assert client.get(dashboard).status_code == 302
    api = reverse("api:access-list", kwargs={"organization_id": organization.pk, "resource": "memberships"})
    assert client.get(api).json()["code"] == "mfa_required"
    assert login(plain).get(dashboard).status_code == 200


def test_sidebar_shows_only_sections_of_the_capability(org):
    owner, organization = org
    people, _ = member_with_role(owner, organization, "pm-menu@example.test", "people_manager")
    page = login(people).get(reverse("web:dashboard", kwargs={"organization_id": organization.pk})).content.decode()
    assert reverse("web:access-list", kwargs={"organization_id": organization.pk, "resource": "memberships"}) in page
    assert reverse("web:access-list", kwargs={"organization_id": organization.pk, "resource": "roles"}) not in page
    assert reverse("application-list", kwargs={"organization_id": organization.pk}) not in page


def test_custom_role_cannot_take_a_reserved_code(skeleton_db):
    from django.core.exceptions import ValidationError

    from skeleton.access.models import Role
    from skeleton.access.services import create_role, update_role
    from tools.security_fixtures import actor_tenant, build_security_fixture

    fixture = build_security_fixture()
    with actor_tenant(fixture.admin, fixture.a):
        custom = create_role(user=fixture.admin, data={"code": "auditors", "name": "Auditors", "description": ""})
        for code in ("app_manager", "organization_admin", "member"):
            with pytest.raises(ValidationError, match="reservado"):
                update_role(Role.objects.get(pk=custom.pk), user=fixture.admin, data={"code": code})
        assert Role.objects.get(pk=custom.pk).code == "auditors"
        renamed = update_role(Role.objects.get(pk=custom.pk), user=fixture.admin, data={"code": "reviewers"})
        assert renamed.code == "reviewers"
        builtin = Role.objects.get(code="member")
        assert update_role(builtin, user=fixture.admin, data={"name": "Members"}).name == "Members"


def test_generic_services_refuse_records_of_another_kind(skeleton_db):
    from skeleton.access.models import AccessGroup, MemberRole, Role
    from skeleton.access.services import delete_group, delete_role, revoke_link, update_group, update_role
    from skeleton.tenancy.models import Membership
    from tools.security_fixtures import actor_tenant, build_security_fixture

    fixture = build_security_fixture()
    with actor_tenant(fixture.admin, fixture.a):
        membership = Membership.objects.get(pk=fixture.bob_membership.pk)
        group = AccessGroup.objects.get(pk=fixture.group.pk)
        role = Role.objects.get(code="member")
        calls = [
            lambda: revoke_link(membership, user=fixture.admin),
            lambda: delete_role(group, user=fixture.admin),
            lambda: delete_group(role, user=fixture.admin),
            lambda: update_role(group, user=fixture.admin, data={"name": "x"}),
            lambda: update_group(role, user=fixture.admin, data={"name": "x"}),
        ]
        for call in calls:
            with pytest.raises(TypeError):
                call()
        assert Membership.objects.get(pk=membership.pk).is_active
        assert AccessGroup.objects.get(pk=group.pk).name == group.name
        assert Role.objects.get(pk=role.pk).name == role.name
        link = MemberRole.objects.filter(membership=membership).first()
        revoke_link(link, user=fixture.admin)
        assert not MemberRole.objects.filter(pk=link.pk).exists()
