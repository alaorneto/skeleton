"""users_who_can_read decides exactly what require_record_action decides, person by person (#296).

Runs on PostgreSQL with the runtime role. Every situation of the read rule gets a person; the
batch answer is compared with the per-person answer for every record kind.
"""

import pytest
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.test.utils import CaptureQueriesContext

from skeleton.access.policies import require_record_action, users_who_can_read
from skeleton.tenancy.context import system_context
from tools.security_fixtures import build_security_fixture

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]
PASSWORD = "Bulk-read-Password-2026!"


def _person(name):
    from skeleton.accounts.services import create_identity

    with system_context(reason="test:bulk-person"):
        return create_identity(email=f"{name}@bulk.example.test", password=PASSWORD)


@pytest.fixture
def world():
    """One person per situation, in organization A of the security fixture."""
    from examples.notes.models import Note
    from skeleton.access.models import (
        AccessGroup,
        GroupMember,
        GroupRole,
        MemberRole,
        RecordGrant,
        Role,
        TablePermission,
    )
    from skeleton.accounts.models import AccountType
    from skeleton.tenancy.models import Membership

    fixture = build_security_fixture()
    names = (
        "no_permission",
        "group_permission",
        "inactive_membership",
        "inactive_user",
        "service",
        "grant_membership",
        "grant_group",
        "grant_role",
        "grant_update_only",
        "admin_by_group",
    )
    people = {name: _person(name) for name in names}
    with system_context(reason="test:bulk-world", organization_id=fixture.a.pk):
        a, table, member = fixture.a, fixture.table, fixture.member_role
        memberships = {name: Membership(organization=a, user=user) for name, user in people.items()}
        for membership in memberships.values():
            membership.save()
        for name in ("inactive_membership", "inactive_user", "service", "grant_membership", "grant_update_only"):
            MemberRole(organization=a, membership=memberships[name], role=member).save()
        reader_role = Role(organization=a, code="bulk-reader", name="Bulk reader")
        reader_role.save()
        TablePermission(organization=a, role=reader_role, table=table, action="read").save()
        via_group = AccessGroup(organization=a, code="bulk-via", name="Via group")
        via_group.save()
        GroupRole(organization=a, group=via_group, role=reader_role).save()
        GroupMember(organization=a, group=via_group, membership=memberships["group_permission"]).save()
        grant_group = AccessGroup(organization=a, code="bulk-grant", name="Grant group")
        grant_group.save()
        GroupRole(organization=a, group=grant_group, role=member).save()
        GroupMember(organization=a, group=grant_group, membership=memberships["grant_group"]).save()
        role_for_grant = Role(organization=a, code="bulk-granted", name="Granted role")
        role_for_grant.save()
        TablePermission(organization=a, role=role_for_grant, table=table, action="read").save()
        MemberRole(organization=a, membership=memberships["grant_role"], role=role_for_grant).save()
        admins = AccessGroup(organization=a, code="bulk-admins", name="Admins")
        admins.save()
        GroupRole(
            organization=a, group=admins, role=Role.objects.get(code="organization_admin", is_builtin=True)
        ).save()
        GroupMember(organization=a, group=admins, membership=memberships["admin_by_group"]).save()
        for _kind, values in (
            ("membership", {"membership": memberships["grant_membership"], "action": "read"}),
            ("group", {"group": grant_group, "action": "read"}),
            ("role", {"role": role_for_grant, "action": "read"}),
            ("update", {"membership": memberships["grant_update_only"], "action": "update"}),
        ):
            RecordGrant(organization=a, table=table, record_sysid=fixture.private.pk, **values).save()
        inactive = memberships["inactive_membership"]
        inactive.is_active = False
        inactive.save(update_fields={"is_active"})
        deleted = Note(
            organization=a, owner=fixture.alice, code="bulk-deleted", title="Gone", visibility="organization"
        )
        deleted.save()
        deleted.delete()
    with system_context(reason="test:bulk-users"):
        user = people["inactive_user"]
        user.is_active = False
        user.save(update_fields={"is_active"})
        service = people["service"]
    assert service.account_type in (AccountType.HUMAN, AccountType.SERVICE)
    everyone = [fixture.admin, fixture.alice, fixture.bob, fixture.outsider, *people.values()]
    return fixture, everyone, deleted


def one_by_one(record, people):
    allowed = set()
    for user in people:
        try:
            require_record_action(record, user=user, action="read")
        except PermissionDenied:
            continue
        allowed.add(user.pk)
    return allowed


def test_bulk_read_matches_require_record_action_for_every_case(world, subtests):
    fixture, everyone, deleted = world
    ids = [user.pk for user in everyone]
    for name, record in (
        ("private", fixture.private),
        ("organization", fixture.shared),
        ("group", fixture.grouped),
        ("deleted", deleted),
    ):
        with subtests.test(name), system_context(reason="test:bulk-compare", organization_id=fixture.a.pk):
            expected = one_by_one(record, everyone)
            assert users_who_can_read(record, ids) == expected, name


def test_bulk_read_cost_does_not_grow_with_people(world):
    fixture, everyone, _ = world
    ids = [user.pk for user in everyone]
    with system_context(reason="test:bulk-cost", organization_id=fixture.a.pk):
        users_who_can_read(fixture.private, ids[:3])
        with CaptureQueriesContext(connection) as few:
            users_who_can_read(fixture.private, ids[:3])
        with CaptureQueriesContext(connection) as many:
            users_who_can_read(fixture.private, ids)
    assert len(many) == len(few)


def test_bulk_read_requires_a_tenant_context(world):
    from skeleton.common.exceptions import MissingTenantContext

    fixture, everyone, _ = world
    with system_context(reason="test:bulk-no-tenant"), pytest.raises(MissingTenantContext):
        users_who_can_read(fixture.private, [user.pk for user in everyone])
