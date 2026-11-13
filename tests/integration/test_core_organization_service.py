"""OrganizationService of the core service API (#103)."""

import pytest

from skeleton.service_api.dto import Page
from skeleton.service_api.errors import InvalidInput, NotFound
from skeleton.service_api.organizations import OrganizationService
from skeleton.tenancy.context import request_context, system_context
from skeleton.testing.service_api import (
    acting_as,
    assert_rejects_other_actor,
    assert_service_contract,
    assert_tenant_isolated,
    build_service_scenario,
)

pytestmark = pytest.mark.usefixtures("skeleton_db")


def test_contract():
    assert_service_contract(OrganizationService)


@pytest.fixture
def scenario():

    from examples.notes.models import Note

    return build_service_scenario(Note, slug="directory")


@pytest.mark.needs_example_app
def test_current_and_my_organizations(scenario):
    from skeleton.tenancy.models import Organization

    service = OrganizationService()
    with acting_as(scenario.member, scenario.a) as actor:
        assert service.current(actor=actor).sysid == scenario.a.pk
    with request_context(actor=scenario.member, request_id=None, source="test:directory-mine"):
        mine = service.my_organizations(actor=scenario.member)
    assert {item.sysid for item in mine} == {scenario.a.pk, scenario.b.pk}
    with system_context(reason="test:directory-deactivate"):
        organization = Organization.objects.get(pk=scenario.b.pk)
        organization.is_active = False
        organization.save(update_fields={"is_active"})
    with request_context(actor=scenario.member, request_id=None, source="test:directory-mine-after"):
        assert [item.sysid for item in service.my_organizations(actor=scenario.member)] == [scenario.a.pk]
    assert_rejects_other_actor(scenario.caller(lambda actor: service.current(actor=scenario.denied)))


@pytest.mark.needs_example_app
def test_directory_never_includes_other_organizations_or_inactive_memberships(scenario):
    from skeleton.tenancy.models import Membership

    service = OrganizationService()
    call = scenario.caller(lambda actor: service.members(actor=actor, page_size=100))
    page = assert_tenant_isolated(call, foreign=[scenario.admin_b.pk])
    assert isinstance(page, Page)
    assert {person.sysid for person in page.items} == {scenario.admin_a.pk, scenario.member.pk, scenario.denied.pk}
    assert all(person.email is None for person in page.items)
    with system_context(reason="test:directory-inactive", organization_id=scenario.a.pk):
        membership = Membership.objects.get(user=scenario.denied)
        membership.is_active = False
        membership.save(update_fields={"is_active"})
    with acting_as(scenario.admin_a, scenario.a) as admin:
        page = service.members(actor=admin)
        assert scenario.denied.pk not in {person.sysid for person in page.items}
        assert all(person.email for person in page.items)  # members.view shows email.
        assert not service.is_member(actor=admin, user_id=scenario.denied.pk)
        assert not service.is_member(actor=admin, user_id=scenario.admin_b.pk)
        assert service.is_member(actor=admin, user_id=scenario.member.pk)


@pytest.mark.needs_example_app
def test_search_group_filter_and_pagination(scenario):
    from skeleton.access.models import AccessGroup, GroupMember
    from skeleton.accounts.models import UserProfile
    from skeleton.tenancy.models import Membership

    with system_context(reason="test:directory-setup", organization_id=scenario.a.pk):
        profile = UserProfile.objects.get(user=scenario.member)
        profile.display_name = "Beatriz Souza"
        profile.save(update_fields={"display_name"})
        group = AccessGroup(organization=scenario.a, code="ops", name="Operações")
        group.save()
        GroupMember(
            organization=scenario.a, group=group, membership=Membership.objects.get(user=scenario.denied)
        ).save()
    with system_context(reason="test:directory-foreign-group", organization_id=scenario.b.pk):
        foreign = AccessGroup(organization=scenario.b, code="ops-b", name="B")
        foreign.save()
    service = OrganizationService()
    with acting_as(scenario.member, scenario.a) as actor:
        assert [person.display_name for person in service.members(actor=actor, query="beatriz").items] == [
            "Beatriz Souza"
        ]
        assert [person.sysid for person in service.members(actor=actor, group_id=group.pk).items] == [
            scenario.denied.pk
        ]
        with pytest.raises(NotFound):
            service.members(actor=actor, group_id=foreign.pk)
        first = service.members(actor=actor, page_size=2)
        rest = service.members(actor=actor, page_size=2, cursor=first.next_cursor)
        assert len(first.items) == 2 and len(rest.items) == 1 and rest.next_cursor is None
        assert not {person.sysid for person in first.items} & {person.sysid for person in rest.items}
        with pytest.raises(InvalidInput):
            service.members(actor=actor, page_size=101)
        groups = service.groups(actor=actor)
        assert [item.code for item in groups.items] == ["ops"]


@pytest.mark.needs_example_app
def test_members_pagination_is_stable_when_a_member_leaves_between_pages(scenario):
    """Keyset pages (#305): a member leaving after page one never makes the walk skip another."""
    from skeleton.access.models import Role
    from skeleton.access.services import provision_membership
    from skeleton.accounts.services import create_identity
    from skeleton.tenancy.models import Membership

    with system_context(reason="test:keyset-members", organization_id=scenario.a.pk):
        role = Role.objects.get(code="member")
        for index in range(3):
            user = create_identity(email=f"keyset-{index}@directory.example.test", password=None)
            provision_membership(organization=scenario.a, user=user, role=role)
    service = OrganizationService()
    with acting_as(scenario.admin_a, scenario.a) as actor:
        everyone = {person.sysid for person in service.members(actor=actor, page_size=100).items}
        first = service.members(actor=actor, page_size=2)
    leaving = next(person.sysid for person in first.items if person.sysid != scenario.admin_a.pk)
    with system_context(reason="test:keyset-leave", organization_id=scenario.a.pk):
        Membership.objects.filter(user_id=leaving).update(is_active=False)
    seen = [person.sysid for person in first.items]
    cursor = first.next_cursor
    with acting_as(scenario.admin_a, scenario.a) as actor:
        while cursor:
            page = service.members(actor=actor, page_size=2, cursor=cursor)
            seen.extend(person.sysid for person in page.items)
            cursor = page.next_cursor
    assert len(seen) == len(set(seen))
    assert set(seen) >= everyone - {leaving}


@pytest.mark.needs_example_app
def test_groups_walk_in_code_order_with_keyset(scenario):
    from skeleton.access.models import AccessGroup

    with system_context(reason="test:keyset-groups", organization_id=scenario.a.pk):
        for code in ("c-group", "a-group", "b-group"):
            AccessGroup(organization=scenario.a, code=code, name=code).save()
    service = OrganizationService()
    codes, cursor = [], None
    with acting_as(scenario.member, scenario.a) as actor:
        while True:
            page = service.groups(actor=actor, page_size=1, cursor=cursor)
            codes.extend(group.code for group in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
    assert codes == sorted(codes) and {"a-group", "b-group", "c-group"} <= set(codes)


@pytest.mark.needs_example_app
def test_tampered_keyset_cursor_is_invalid_input(scenario):
    from skeleton.service_api.dto import encode_key_cursor

    service = OrganizationService()
    with acting_as(scenario.member, scenario.a) as actor:
        with pytest.raises(InvalidInput):
            service.members(actor=actor, cursor=encode_key_cursor(["not-a-uuid"]))
        # A cursor positioned at a person of B only moves inside A's authorized members.
        page = service.members(actor=actor, page_size=100, cursor=encode_key_cursor([str(scenario.admin_b.pk)]))
    assert scenario.admin_b.pk not in {person.sysid for person in page.items}
