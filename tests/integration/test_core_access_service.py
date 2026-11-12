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
