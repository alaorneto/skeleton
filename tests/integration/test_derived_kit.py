"""The derived access contract kit (#566) on the tables of the recipe in docs/extending.md, one and two hops."""

import pytest

from skeleton.testing.derived import assert_derived_access_contract, create_group, grant_anchor
from skeleton.testing.service_api import acting_as, build_service_scenario
from tests.integration.derived_support import derived_tables

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]


@pytest.fixture
def tables(owner_schema_editor):
    with derived_tables(owner_schema_editor) as tables:
        yield tables


def make_anchor(tables):
    def make(*, organization, owner):
        anchor = tables.workspace(organization=organization, owner=owner, visibility="private", title="kit")
        anchor.save()
        return anchor

    return make


def test_the_kit_passes_for_one_and_two_hops(tables):
    scenario = build_service_scenario(tables.item, slug="derived-kit")
    assert scenario.anchor_model is tables.workspace and scenario.model is tables.item

    def make_item(anchor):
        item = tables.item(organization=anchor.organization, workspace=anchor, title="item")
        item.save()
        return item

    def make_line(anchor):
        line = tables.line(organization=anchor.organization, item=make_item(anchor), text="line")
        line.save()
        return line

    assert_derived_access_contract(
        child_model=tables.item, make_anchor=make_anchor(tables), make_child=make_item, scenario=scenario
    )
    assert_derived_access_contract(
        child_model=tables.line, make_anchor=make_anchor(tables), make_child=make_line, scenario=scenario
    )


def test_a_group_grant_on_the_anchor_reaches_its_members(tables):
    from skeleton.access.policies import authorized_queryset

    scenario = build_service_scenario(tables.workspace, slug="derived-kit-group")
    anchor = scenario.create(scenario.a, owner=scenario.admin_a, visibility="private", title="team")
    with acting_as(scenario.admin_a, scenario.a):
        item = tables.item(organization=scenario.a, workspace=anchor, title="team item")
        item.save()
    team = create_group(scenario, "Equipe", [scenario.member])
    grant_anchor(scenario, anchor, group=team, action="read")
    with acting_as(scenario.member, scenario.a) as actor:
        assert authorized_queryset(tables.item, user=actor).filter(pk=item.pk).exists()
    with pytest.raises(TypeError):
        grant_anchor(scenario, anchor, action="read")


def test_the_kit_refuses_a_table_that_is_not_derived(tables):
    scenario = build_service_scenario(tables.workspace, slug="derived-kit-refuse")
    with pytest.raises(AssertionError, match="not a valid derived table"):
        assert_derived_access_contract(
            child_model=tables.workspace,
            make_anchor=make_anchor(tables),
            make_child=lambda anchor: anchor,
            scenario=scenario,
        )
