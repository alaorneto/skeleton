"""AccessService of the core service API (#104)."""

import pytest
from django.core.exceptions import PermissionDenied

from skeleton.access.policies import ACTIONS, authorized_queryset
from skeleton.service_api.access import AccessService
from skeleton.service_api.errors import Forbidden, InvalidInput, NotFound
from skeleton.tenancy.context import system_context
from skeleton.testing.service_api import (
    acting_as,
    assert_rejects_other_actor,
    assert_service_contract,
    build_service_scenario,
)

pytestmark = pytest.mark.usefixtures("skeleton_db")
TABLE = "example_notes.note"


def test_contract():
    assert_service_contract(AccessService)


@pytest.fixture
def scenario():

    from examples.notes.models import Note

    return build_service_scenario(Note, slug="access")


def expected(model, user, action, record):
    try:
        queryset = authorized_queryset(model, user=user, action=action)
    except PermissionDenied:
        return False
    return queryset.filter(pk=record.pk).exists() if record is not None else True


@pytest.mark.needs_example_app
def test_can_matches_authorized_queryset_on_a_role_by_action_matrix(scenario):
    own = scenario.create(scenario.a, owner=scenario.member, code="AC-1", title="Own", visibility="private")
    shared = scenario.create(scenario.a, owner=scenario.admin_a, code="AC-2", title="Org", visibility="organization")
    hidden = scenario.create(scenario.a, owner=scenario.admin_a, code="AC-3", title="Private", visibility="private")
    service = AccessService()
    for user in (scenario.member, scenario.denied, scenario.admin_a):
        with acting_as(user, scenario.a) as actor:
            for action in sorted(ACTIONS):
                for record in (None, own, shared, hidden):
                    got = service.can(
                        actor=actor, action=action, table_key=TABLE, record_id=getattr(record, "pk", None)
                    )
                    want = expected(scenario.model, actor, action, record)
                    assert got == want, (user.email, action, getattr(record, "code", None))


@pytest.mark.needs_example_app
def test_foreign_and_unknown_targets_are_false_and_require_raises_not_found(scenario):
    foreign = scenario.create(scenario.b, owner=scenario.member, code="AC-4", title="B")
    shared = scenario.create(scenario.a, owner=scenario.admin_a, code="AC-5", title="Org", visibility="organization")
    service = AccessService()
    with acting_as(scenario.member, scenario.a) as actor:
        assert not service.can(actor=actor, action="read", table_key=TABLE, record_id=foreign.pk)
        assert not service.can(actor=actor, action="read", table_key="sk_accounts.user")
        assert not service.can(actor=actor, action="read", table_key="no.such")
        with pytest.raises(NotFound):
            service.require(actor=actor, action="read", table_key=TABLE, record_id=foreign.pk)
        with pytest.raises(Forbidden):
            service.require(actor=actor, action="update", table_key=TABLE, record_id=shared.pk)
        service.require(actor=actor, action="read", table_key=TABLE, record_id=shared.pk)
        with pytest.raises(InvalidInput):
            service.can(actor=actor, action="explode", table_key=TABLE)
    with acting_as(scenario.denied, scenario.a) as denied, pytest.raises(Forbidden):
        service.require(actor=denied, action="read", table_key=TABLE)


@pytest.mark.needs_example_app
def test_revocation_applies_to_the_next_call(scenario):
    from skeleton.access.models import MemberRole

    note = scenario.create(scenario.a, owner=scenario.admin_a, code="AC-6", title="Org", visibility="organization")
    service = AccessService()
    with acting_as(scenario.member, scenario.a) as actor:
        assert service.can(actor=actor, action="read", table_key=TABLE, record_id=note.pk)
    with system_context(reason="test:access-revoke", organization_id=scenario.a.pk):
        MemberRole.objects.filter(membership__user=scenario.member, role__code="service-contract").delete()
    with acting_as(scenario.member, scenario.a) as actor:
        assert not service.can(actor=actor, action="read", table_key=TABLE, record_id=note.pk)


@pytest.mark.needs_example_app
def test_capabilities_and_groups(scenario):
    from skeleton.access.models import AccessGroup, GroupMember
    from skeleton.tenancy.models import Membership

    with system_context(reason="test:access-group", organization_id=scenario.a.pk):
        group = AccessGroup(organization=scenario.a, code="team", name="Team")
        group.save()
        GroupMember(
            organization=scenario.a, group=group, membership=Membership.objects.get(user=scenario.member)
        ).save()
    service = AccessService()
    with acting_as(scenario.member, scenario.a) as actor:
        assert service.capabilities(actor=actor) == ()
        assert [item.code for item in service.my_groups(actor=actor)] == ["team"]
    with acting_as(scenario.admin_a, scenario.a) as admin:
        assert "access.manage" in service.capabilities(actor=admin)
        assert service.my_groups(actor=admin) == ()
    assert_rejects_other_actor(scenario.caller(lambda actor: service.capabilities(actor=scenario.denied)))


def _member_permissions(organization):
    from skeleton.access.models import Role, TablePermission

    with system_context(reason="test:member-permissions", organization_id=organization.pk):
        role = Role.objects.get(code="member", is_builtin=True)
        return set(TablePermission.objects.filter(role=role).values_list("table__model_label", "action"))


@pytest.mark.needs_example_app
def test_grant_member_table_access_requires_access_manage(scenario):
    before = _member_permissions(scenario.a)
    with acting_as(scenario.member, scenario.a) as actor, pytest.raises(Forbidden):
        AccessService().grant_member_table_access(actor=actor, table_key=TABLE, actions=("read",))
    assert _member_permissions(scenario.a) == before


@pytest.mark.needs_example_app
def test_grant_member_table_access_adds_only_missing_actions(scenario):
    service = AccessService()
    with acting_as(scenario.admin_a, scenario.a) as admin:
        assert service.grant_member_table_access(actor=admin, table_key=TABLE, actions=("read", "create")) == (
            "read",
            "create",
        )
    after = _member_permissions(scenario.a)
    with acting_as(scenario.admin_a, scenario.a) as admin:
        assert service.grant_member_table_access(actor=admin, table_key=TABLE, actions=("read", "create")) == ()
    assert _member_permissions(scenario.a) == after >= {(TABLE, "read"), (TABLE, "create")}


@pytest.mark.needs_example_app
def test_grant_member_table_access_rejects_unknown_table_and_action(scenario):
    service = AccessService()
    with acting_as(scenario.admin_a, scenario.a) as admin:
        for arguments in (
            {"table_key": "nope.nope", "actions": ("read",)},
            {"table_key": TABLE, "actions": ("fly",)},
            {"table_key": TABLE, "actions": ()},
        ):
            with pytest.raises(InvalidInput):
                service.grant_member_table_access(actor=admin, **arguments)


@pytest.mark.needs_example_app
def test_grant_member_table_access_stays_in_the_organization(scenario):
    before_b = _member_permissions(scenario.b)
    with acting_as(scenario.admin_a, scenario.a) as admin:
        AccessService().grant_member_table_access(actor=admin, table_key=TABLE, actions=("read",))
    assert _member_permissions(scenario.b) == before_b


@pytest.mark.needs_example_app
def test_grant_member_table_access_rejects_other_actor(scenario):
    assert_rejects_other_actor(
        scenario.caller(
            lambda actor: AccessService().grant_member_table_access(
                actor=scenario.denied, table_key=TABLE, actions=("read",)
            ),
            user=scenario.admin_a,
        )
    )


@pytest.mark.needs_example_app
def test_disabled_table_app_denies_can_and_require(scenario):
    """The table authorization includes the activation of its app (#319): no answer reveals data."""
    note = scenario.create(scenario.a, owner=scenario.member, code="ACC-OFF", title="T")
    service = AccessService()
    with scenario.application_disabled(scenario.a):
        with acting_as(scenario.member, scenario.a) as actor:
            assert service.can(actor=actor, action="read", table_key=TABLE) is False
            assert service.can(actor=actor, action="read", table_key=TABLE, record_id=note.pk) is False
            with pytest.raises((NotFound, Forbidden)):
                service.require(actor=actor, action="read", table_key=TABLE, record_id=note.pk)
