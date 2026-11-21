"""People, organizations, memberships, roles and groups of the test kit (#572), on the runtime role."""

import shutil
from pathlib import Path

import pytest

from skeleton.system.import_boundary import scan_application
from skeleton.tenancy.context import system_context
from skeleton.testing.people import FIXTURE_PASSWORD, add_member, make_group, make_person, make_role
from tests.integration.people_kit_scenario import TEAMS, build_teams

SCENARIO = Path(__file__).with_name("people_kit_scenario.py")


def _memberships(organization):
    from skeleton.access.models import MemberRole
    from skeleton.tenancy.models import Membership

    with system_context(reason="test:people-kit", organization_id=organization.pk):
        active = set(Membership.objects.filter(is_active=True).values_list("user_id", flat=True))
        roles = {(user, code) for user, code in MemberRole.objects.values_list("membership__user_id", "role__code")}
    return active, roles


def _group_members(group):
    from skeleton.access.models import GroupMember

    with system_context(reason="test:people-kit", organization_id=group.organization_id):
        return set(GroupMember.objects.filter(group=group).values_list("membership__user_id", flat=True))


def test_two_organizations_with_four_people_and_three_groups(skeleton_db):
    sides = build_teams("people-kit")
    a, b = sides["a"], sides["b"]
    for side, other in ((a, b), (b, a)):
        active, roles = _memberships(side.organization)
        assert active == {person.pk for person in side.people.values()}
        assert not active & {person.pk for person in other.people.values()}, "a person of one side joined the other"
        assert (side.people["owner"].pk, "organization_admin") in roles
        for team in TEAMS:
            person = side.people[team]
            assert {(person.pk, "member"), (person.pk, team)} <= roles
            group = side.groups[team]
            assert group.organization_id == side.organization.pk
            expected = {person.pk, side.people["owner"].pk} if team == "finance" else {person.pk}
            assert _group_members(group) == expected
    person = a.people["sales"]
    assert person.check_password(FIXTURE_PASSWORD)
    assert person.first_name == "Sales"
    assert person.emailaddress_set.get().verified


def test_the_scenario_needs_no_internal_import(tmp_path):
    """An app whose tests build this scenario stays inside the public surfaces (no E019/W019)."""
    package = tmp_path / "teams"
    (package / "tests").mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "tests" / "__init__.py").write_text("", encoding="utf-8")
    shutil.copyfile(SCENARIO, package / "tests" / "scenario.py")
    assert scan_application(package, package="applications.teams", providers=[], dependencies=[]) == []
    (package / "tests" / "internal.py").write_text("from skeleton.access.services import create_group\n", "utf-8")
    assert [
        item.code for item in scan_application(package, package="applications.teams", providers=[], dependencies=[])
    ] == ["core_import"]


def test_helpers_refuse_what_the_services_refuse(skeleton_db):
    from skeleton.testing.people import make_organization

    owner = make_person(email="owner@people-refusals.example.test")
    other_owner = make_person(email="other@people-refusals.example.test")
    outsider = make_person(email="outsider@people-refusals.example.test")
    organization = make_organization(owner=owner, slug="people-refusals-a", name="Refusals A")
    other = make_organization(owner=other_owner, slug="people-refusals-b", name="Refusals B")
    with pytest.raises(LookupError, match="make_role"):
        add_member(organization=organization, user=outsider, role_code="missing")
    with pytest.raises(LookupError, match="add_member"):
        make_group(organization=organization, name="Strangers", members=[outsider])
    with pytest.raises(LookupError, match="add_member"):
        make_group(organization=other, name="Other side", members=[owner])
    from django.core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        make_role(organization=organization, code="member", name="Builtin code")
