"""Derived tables authorized through their parent record (#561): the access matrix, A/B and tombstones.

Runs on PostgreSQL with the runtime role. ``Workspace`` is the exposed anchor, ``WorkItem`` derives
from it (``access_parent="workspace"``) and ``WorkItemLine`` from the item (two hops). One
preparation per test; the read cases share it through ``subtests``.
"""

from uuid import uuid4

import pytest
from django.core.exceptions import PermissionDenied

from skeleton.access.derivation import access_route, is_authorizable, is_derived
from skeleton.access.policies import (
    authorized_queryset,
    require_parent_action,
    require_record_action,
    users_who_can_read,
)
from skeleton.tenancy.context import request_context, system_context, tenant_context
from skeleton.testing.service_api import acting_as, build_service_scenario
from tests.integration.derived_support import child, derived_tables, grant, reader_only, tombstone

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]


@pytest.fixture
def world(owner_schema_editor):
    with derived_tables(owner_schema_editor) as tables:
        scenario = build_service_scenario(tables.workspace, slug="derived")
        admin = scenario.admin_a
        spaces = {
            "own": scenario.create(scenario.a, owner=scenario.member, visibility="private", title="own"),
            "private": scenario.create(scenario.a, owner=admin, visibility="private", title="private"),
            "organization": scenario.create(scenario.a, owner=admin, visibility="organization", title="org"),
            "read_grant": scenario.create(scenario.a, owner=admin, visibility="private", title="read grant"),
            "update_grant": scenario.create(scenario.a, owner=admin, visibility="private", title="update grant"),
            "gone": scenario.create(scenario.a, owner=scenario.member, visibility="private", title="gone"),
        }
        grant(scenario, spaces["read_grant"], user=scenario.member, action="read")
        grant(scenario, spaces["update_grant"], user=scenario.member, action="read")
        grant(scenario, spaces["update_grant"], user=scenario.member, action="update")
        items = {name: child(tables.item, scenario.a, workspace=space, title=name) for name, space in spaces.items()}
        lines = {name: child(tables.line, scenario.a, item=item, text=name) for name, item in items.items()}
        tombstone(tables.workspace, spaces["gone"], scenario.a)
        foreign_space = scenario.create(scenario.b, owner=scenario.member, visibility="organization", title="beta")
        foreign_item = child(tables.item, scenario.b, workspace=foreign_space, title="beta item")
        foreign_line = child(tables.line, scenario.b, item=foreign_item, text="beta line")
        yield tables, scenario, spaces, items, lines, (foreign_space, foreign_item, foreign_line)


def _visible(model, user, organization, action, **options):
    with acting_as(user, organization):
        return set(authorized_queryset(model, user=user, action=action, **options).values_list("sysid", flat=True))


def test_routes_and_the_public_predicates(world):
    tables, *_ = world
    assert access_route(tables.workspace).path == ""
    assert access_route(tables.item).path == "workspace" and access_route(tables.item).links == ()
    route = access_route(tables.line)
    assert (route.anchor, route.path, route.links) == (tables.workspace, "item__workspace", ("item",))
    assert is_authorizable(tables.line) and is_derived(tables.line) and not is_derived(tables.workspace)


def test_the_anchor_decides_reading_and_writing_of_every_hop(world, subtests):
    tables, scenario, spaces, items, lines, foreign = world
    member = scenario.member
    expected = {
        "read": {"own", "organization", "read_grant", "update_grant"},
        "update": {"own", "update_grant"},
        "delete": {"own", "update_grant"},
        "create": {"own", "update_grant"},
    }
    for action, names in expected.items():
        with subtests.test(action=action, table="item"):
            assert _visible(tables.item, member, scenario.a, action) == {items[name].pk for name in names}
        with subtests.test(action=action, table="line"):
            assert _visible(tables.line, member, scenario.a, action) == {lines[name].pk for name in names}
    with subtests.test("the organization administrator reads every live child"):
        assert _visible(tables.item, scenario.admin_a, scenario.a, "read") == {
            item.pk for name, item in items.items() if name != "gone"
        }
    with subtests.test("require_record_action follows the same rule"):
        with acting_as(member, scenario.a):
            require_record_action(items["read_grant"], user=member, action="read")
            with pytest.raises(PermissionDenied):
                require_record_action(items["read_grant"], user=member, action="update")
            with pytest.raises(PermissionDenied):
                require_record_action(items["organization"], user=member, action="update")
            require_record_action(lines["update_grant"], user=member, action="update")
            with pytest.raises(PermissionDenied):
                require_record_action(items["private"], user=member, action="read")
    with subtests.test("manage_access is always managed on the anchor"):
        with acting_as(scenario.admin_a, scenario.a), pytest.raises(PermissionDenied, match="parent"):
            authorized_queryset(tables.item, user=scenario.admin_a, action="manage_access")
    with subtests.test("include_deleted only with restore"):
        with acting_as(member, scenario.a), pytest.raises(PermissionDenied, match="restore"):
            authorized_queryset(tables.item, user=member, action="read", include_deleted=True)
    foreign_space, foreign_item, foreign_line = foreign
    with subtests.test("organization B never shows in A"):
        with acting_as(member, scenario.a):
            rows = authorized_queryset(tables.item, user=member, action="read")
            assert not rows.filter(sysid=foreign_item.pk).exists()
            assert rows.count() == len(expected["read"])
            assert not authorized_queryset(tables.line, user=member, action="read").filter(sysid=foreign_line.pk)
    with subtests.test("organization B reads its own"):
        assert _visible(tables.item, member, scenario.b, "read") == {foreign_item.pk}


def test_table_permission_of_the_anchor_is_required(world, subtests):
    tables, scenario, spaces, items, lines, _ = world
    owned = scenario.create(scenario.a, owner=scenario.denied, visibility="private", title="denied own")
    child(tables.item, scenario.a, workspace=owned, title="denied item")
    reader = reader_only(scenario, tables.workspace)
    with subtests.test("no permission at all, even as the owner"):
        with acting_as(scenario.denied, scenario.a), pytest.raises(PermissionDenied, match="Table permission"):
            authorized_queryset(tables.item, user=scenario.denied, action="read")
    with subtests.test("read permission reads, never writes"):
        assert _visible(tables.item, reader, scenario.a, "read") == {items["organization"].pk}
        with acting_as(reader, scenario.a), pytest.raises(PermissionDenied, match="Table permission"):
            authorized_queryset(tables.item, user=reader, action="update")
    with subtests.test("the parent check for a new child"):
        with acting_as(scenario.member, scenario.a):
            assert require_parent_action(tables.item, spaces["update_grant"], user=scenario.member) is not None
            require_parent_action(tables.line, items["own"], user=scenario.member)
            with pytest.raises(PermissionDenied):
                require_parent_action(tables.item, spaces["read_grant"], user=scenario.member)
            with pytest.raises(PermissionDenied, match="access parent"):
                require_parent_action(tables.line, spaces["own"], user=scenario.member)
            with pytest.raises(PermissionDenied, match="not a derived"):
                require_parent_action(tables.workspace, spaces["own"], user=scenario.member)


def test_a_token_scope_of_the_anchor_narrows_thechild(world):
    tables, scenario, spaces, items, *_ = world
    member = scenario.member
    scopes = frozenset({f"{tables.workspace._meta.label_lower}:read"})
    with request_context(actor=member, request_id=uuid4(), source="test:derived-token", credential_scopes=scopes):
        with tenant_context(organization_id=scenario.a.pk, actor=member, source="test:derived-token"):
            assert authorized_queryset(tables.item, user=member, action="read").filter(sysid=items["own"].pk)
            with pytest.raises(PermissionDenied, match="Table permission"):
                authorized_queryset(tables.item, user=member, action="update")


def test_a_disabled_app_refuses_like_an_ordinary_table(world):
    tables, scenario, *_ = world
    with scenario.application_disabled():
        with acting_as(scenario.member, scenario.a):
            with pytest.raises(PermissionDenied) as ordinary:
                authorized_queryset(tables.workspace, user=scenario.member)
            with pytest.raises(PermissionDenied) as derived:
                authorized_queryset(tables.item, user=scenario.member)
    assert type(derived.value) is type(ordinary.value)
    assert str(derived.value) == str(ordinary.value)


def test_tombstones_of_the_anchor_and_of_a_link_hide_the_children(world, subtests):
    tables, scenario, spaces, items, lines, _ = world
    member = scenario.member
    with subtests.test("a tombstoned anchor hides its live children"):
        assert items["gone"].pk not in _visible(tables.item, member, scenario.a, "read")
        assert lines["gone"].pk not in _visible(tables.line, member, scenario.a, "update")
    tombstone(tables.item, items["own"], scenario.a)
    with subtests.test("a tombstoned item hides its lines"):
        assert lines["own"].pk not in _visible(tables.line, member, scenario.a, "read")
    with subtests.test("restore sees deleted children of live parents only"):
        space = spaces["update_grant"]
        doomed = child(tables.item, scenario.a, workspace=space, title="doomed")
        with system_context(reason="fixture:derived-delete", organization_id=scenario.a.pk):
            doomed.delete()
        restorable = _visible(tables.item, member, scenario.a, "restore", include_deleted=True)
        assert doomed.pk in restorable
        assert items["gone"].pk not in restorable  # The anchor itself is gone: restore it first.
        assert lines["own"].pk not in _visible(tables.line, member, scenario.a, "restore", include_deleted=True)


def test_users_who_can_read_a_child_are_those_of_the_anchor(world, subtests):
    tables, scenario, spaces, items, lines, _ = world
    people = [scenario.member, scenario.denied, scenario.admin_a, scenario.outsider]
    ids = [person.pk for person in people]
    with acting_as(scenario.admin_a, scenario.a):
        for name in ("own", "private", "organization", "read_grant"):
            with subtests.test(workspace=name):
                assert users_who_can_read(items[name], ids) == users_who_can_read(spaces[name], ids)
                assert users_who_can_read(lines[name], ids) == users_who_can_read(spaces[name], ids)
        with subtests.test("empty when the anchor has a tombstone"):
            assert users_who_can_read(items["gone"], ids) == set()
            assert users_who_can_read(lines["gone"], ids) == set()
