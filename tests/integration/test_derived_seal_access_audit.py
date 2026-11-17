"""Seal, AccessService and the audit history of rows of a derived table (#564).

Runs on PostgreSQL with the runtime role. ``WorkItem`` is sealable here (``seal_fields=("title",)``)
and its lines are frozen by its seal (``seal_parent="item"``); the owner only tampers with the
rows, with the guards turned off, to prove that ``verify`` notices.
"""

import pytest
from django.core.exceptions import PermissionDenied

from skeleton.common.sealing import digest
from skeleton.records.sealing import seal_record
from skeleton.service_api.access import AccessService
from skeleton.service_api.audit import AuditService
from skeleton.service_api.errors import Conflict, Forbidden, InvalidInput, NotFound
from skeleton.service_api.records import RecordSealService
from skeleton.tenancy.context import system_context
from skeleton.testing.sealing import assert_sealed_contract
from skeleton.testing.service_api import acting_as, build_service_scenario
from tests.integration.derived_support import child, derived_tables, grant, reader_only

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]
ITEM = "example_notes.workitem"
LINE = "example_notes.workitemline"


@pytest.fixture
def world(owner_schema_editor):
    with derived_tables(owner_schema_editor, sealable=True) as tables:
        scenario = build_service_scenario(tables.workspace, slug="derived-seal")
        admin = scenario.admin_a
        spaces = {
            "writable": scenario.create(scenario.a, owner=admin, visibility="private", title="writable"),
            "readable": scenario.create(scenario.a, owner=admin, visibility="organization", title="readable"),
            "hidden": scenario.create(scenario.a, owner=admin, visibility="private", title="hidden"),
        }
        grant(scenario, spaces["writable"], user=scenario.member, action="read")
        grant(scenario, spaces["writable"], user=scenario.member, action="update")
        items = {name: child(tables.item, scenario.a, workspace=space, title=name) for name, space in spaces.items()}
        yield tables, scenario, spaces, items, owner_schema_editor


def lines_digest(item, tables):
    """The app's own digest of the item's lines, passed as ``extra_digest``."""
    rows = tables.line.all_objects.filter(item=item).order_by("sysid").values_list("sysid", "text")
    return digest([[str(sysid), text] for sysid, text in rows])


def tamper(owner_schema_editor, table, column, value, pk):
    with owner_schema_editor() as editor:
        editor.execute(f"ALTER TABLE skeleton.{table} DISABLE TRIGGER USER")
        editor.execute(f"UPDATE skeleton.{table} SET {column} = %s WHERE sysid = %s", [value, pk])
        editor.execute(f"ALTER TABLE skeleton.{table} ENABLE TRIGGER USER")


def test_the_seal_service_authorizes_through_the_anchor(world):
    tables, scenario, spaces, items, _ = world
    service = RecordSealService()
    with acting_as(scenario.member, scenario.a) as actor:
        with pytest.raises(Forbidden):  # Reads the anchor, cannot update it.
            service.seal(actor=actor, table_key=ITEM, record_id=items["readable"].pk, expected_revision=1)
        with pytest.raises(NotFound):
            service.seal(actor=actor, table_key=ITEM, record_id=items["hidden"].pk, expected_revision=1)
        with pytest.raises(Conflict) as stale:
            service.seal(actor=actor, table_key=ITEM, record_id=items["writable"].pk, expected_revision=5)
        assert stale.value.code == "stale_revision"
        with pytest.raises(InvalidInput):  # Not sealable.
            service.seal(actor=actor, table_key=LINE, record_id=items["writable"].pk, expected_revision=1)
    with acting_as(scenario.member, scenario.a) as actor:
        info = service.seal(actor=actor, table_key=ITEM, record_id=items["writable"].pk, expected_revision=1)
        assert info.sealed and service.status(actor=actor, table_key=ITEM, record_id=items["writable"].pk).sealed
    with acting_as(scenario.denied, scenario.a) as actor, pytest.raises(NotFound):
        service.status(actor=actor, table_key=ITEM, record_id=items["writable"].pk)
    with acting_as(scenario.member, scenario.a) as actor, pytest.raises(PermissionDenied):
        seal_record(items["readable"], user=actor, expected_revision=1)  # In process: the same update check.


def test_verify_notices_tampering_of_the_child_and_of_its_lines(world):
    tables, scenario, spaces, items, owner_schema_editor = world
    item = items["writable"]
    line = child(tables.line, scenario.a, item=item, text="original")
    service = RecordSealService()
    with acting_as(scenario.member, scenario.a) as actor:
        extra = lines_digest(item, tables)
        service.seal(actor=actor, table_key=ITEM, record_id=item.pk, expected_revision=1, extra_digest=extra)
    with acting_as(scenario.member, scenario.a) as actor:
        assert service.verify(actor=actor, table_key=ITEM, record_id=item.pk, extra_digest=extra).valid
    tamper(owner_schema_editor, tables.line._meta.db_table, "text", "forged", line.pk)
    with acting_as(scenario.member, scenario.a) as actor:
        changed = lines_digest(item, tables)
        assert changed != extra
        assert not service.verify(actor=actor, table_key=ITEM, record_id=item.pk, extra_digest=changed).valid
    tamper(owner_schema_editor, tables.item._meta.db_table, "title", "forged", item.pk)
    with acting_as(scenario.member, scenario.a) as actor:
        assert not service.verify(actor=actor, table_key=ITEM, record_id=item.pk, extra_digest=extra).valid


def test_access_service_answers_as_the_anchor_decides(world, subtests):
    tables, scenario, spaces, items, _ = world
    service = AccessService()
    with acting_as(scenario.member, scenario.a) as actor:
        for name, expected in (("writable", True), ("readable", False), ("hidden", False)):
            with subtests.test(item=name):
                record = items[name].pk
                assert service.can(actor=actor, action="update", table_key=ITEM, record_id=record) is expected
                assert service.can(actor=actor, action="manage_access", table_key=ITEM, record_id=record) is False
        with subtests.test("table level"):
            assert service.can(actor=actor, action="read", table_key=ITEM) is True
            assert service.can(actor=actor, action="manage_access", table_key=ITEM) is False
        with subtests.test("require"):
            service.require(actor=actor, action="update", table_key=ITEM, record_id=items["writable"].pk)
            with pytest.raises(Forbidden):
                service.require(actor=actor, action="update", table_key=ITEM, record_id=items["readable"].pk)
            with pytest.raises(NotFound):
                service.require(actor=actor, action="read", table_key=ITEM, record_id=items["hidden"].pk)
        with subtests.test("granted ids come from the anchor"):
            wanted = tuple(item.pk for item in items.values())
            assert service.granted_record_ids(actor=actor, table_key=ITEM, record_ids=wanted, action="update") == (
                items["writable"].pk,
            )
    with acting_as(scenario.denied, scenario.a) as actor:
        assert service.can(actor=actor, action="read", table_key=ITEM) is False
    with acting_as(scenario.admin_a, scenario.a) as admin, pytest.raises(InvalidInput) as refused:
        service.grant_record_access(
            actor=admin,
            table_key=ITEM,
            record_id=items["hidden"].pk,
            action="read",
            membership_user_id=scenario.member.pk,
        )
    assert "âncora" in str(refused.value.errors)


def test_the_audit_history_of_a_child_needs_audit_on_the_anchor(world):
    from skeleton.access.models import Role, TablePermission
    from skeleton.system.models import TableDefinition

    tables, scenario, spaces, items, _ = world
    item = items["readable"]
    with system_context(reason="fixture:derived-audit", organization_id=scenario.a.pk):
        item.status = "closed"
        item.save()
    reader = reader_only(scenario, tables.workspace)
    service = AuditService()
    with acting_as(reader, scenario.a) as actor, pytest.raises(NotFound):
        service.history(actor=actor, table_key=ITEM, record_id=item.pk)
    with system_context(reason="fixture:derived-audit-role", organization_id=scenario.a.pk):
        role = Role.objects.get(code="derived-reader")
        table = TableDefinition.objects.get(model_label=tables.workspace._meta.label_lower)
        TablePermission(organization=scenario.a, role=role, table=table, action="audit").save()
    with acting_as(reader, scenario.a) as actor:
        entries = service.history(actor=actor, table_key=ITEM, record_id=item.pk).items
        assert [entry.operation for entry in entries][-1] == "create" and len(entries) == 2
        # Only the readable columns of the derived table, never the system or seal columns.
        assert {change.name for entry in entries for change in entry.changes} <= {"workspace_id", "title", "status"}
        assert any(change.name == "status" and change.new == "closed" for change in entries[0].changes)
        with pytest.raises(NotFound):  # Audit on the anchor table, but no read of that anchor record.
            service.history(actor=actor, table_key=ITEM, record_id=items["hidden"].pk)
    with acting_as(scenario.admin_a, scenario.a) as admin:
        assert len(service.history(actor=admin, table_key=ITEM, record_id=item.pk).items) == 2


def test_the_seal_kit_passes_for_a_derived_sealable_table(world):
    tables, scenario, spaces, items, _ = world

    def make_record():
        record = tables.item(organization=scenario.a, workspace=spaces["writable"], title="kit")
        record.save()
        return record

    def make_child(record):
        line = tables.line(organization=scenario.a, item=record, text="late")
        line.save()
        return line

    assert_sealed_contract(
        tables.item,
        make_record=make_record,
        seal=lambda record: seal_record(record, user=scenario.member, expected_revision=record.revision),
        acting=scenario.as_member,
        foreign=lambda: scenario.acting(scenario.admin_b, scenario.b),
        make_child=make_child,
    )
