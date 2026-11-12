"""sync_table_catalog reads the catalog once and writes only what changed (#197)."""

from django.db import connection
from django.test.utils import CaptureQueriesContext

PROBE = "zz_catalog_probe"


def _catalog():
    from skeleton.system.models import FieldDefinition, TableDefinition
    from skeleton.tenancy.context import system_context

    with system_context(reason="test:catalog"):
        tables = {
            row.db_table: (row.pk, row.revision, row.available, row.label, row.origin)
            for row in TableDefinition.all_objects.all()
        }
        fields = {
            (row.table.db_table, row.column): (row.pk, row.revision, row.available, row.label, row.primary_key)
            for row in FieldDefinition.all_objects.select_related("table")
        }
    return tables, fields


def test_unchanged_catalog_is_read_in_two_queries_and_not_written(skeleton_db):
    from skeleton.system.synchronization import sync_table_catalog

    before = _catalog()
    with CaptureQueriesContext(connection) as queries:
        result = sync_table_catalog()
    statements = [query["sql"].lstrip() for query in queries]

    assert result == {"tables": len(before[0]), "fields": len(before[1])}
    assert not [sql for sql in statements if sql.upper().startswith(("INSERT", "UPDATE", "DELETE"))]
    assert _catalog() == before, "No row, revision or text changes when the schema did not."
    # One read per catalog table, however many tables and columns exist.
    assert len([sql for sql in statements if sql.startswith('SELECT "sk_system_tabledefinition"."sysid"')]) == 1
    assert len([sql for sql in statements if sql.startswith('SELECT "sk_system_fielddefinition"."sysid"')]) == 1
    assert len(statements) < len(before[1]), "The synchronization must not query once per column."


def test_new_changed_and_removed_tables_and_columns_are_reconciled(skeleton_db, owner_schema_editor):
    from skeleton.system.models import FieldDefinition
    from skeleton.system.synchronization import sync_table_catalog
    from skeleton.tenancy.context import system_context

    sync_table_catalog()
    untouched_tables, untouched_fields = _catalog()

    with owner_schema_editor() as editor:
        editor.execute(f"CREATE TABLE skeleton.{PROBE} (id integer PRIMARY KEY, note text, extra integer NOT NULL)")
        editor.execute(f"GRANT SELECT ON skeleton.{PROBE} TO skeleton_runtime")
    sync_table_catalog()
    tables, fields = _catalog()
    table_id, _, available, label, origin = tables[PROBE]
    assert (available, label, origin) == (True, PROBE, "unclassified")
    assert {column for name, column in fields if name == PROBE} == {"id", "note", "extra"}
    assert fields[PROBE, "id"][4] is True and fields[PROBE, "note"][4] is False
    assert {name: row for name, row in tables.items() if name != PROBE} == untouched_tables
    assert {key: row for key, row in fields.items() if key[0] != PROBE} == untouched_fields

    # A human text survives; a physical change (nullability) is written once.
    with system_context(reason="test:catalog"):
        note = FieldDefinition.all_objects.get(pk=fields[PROBE, "note"][0])
        note.label = "Human note"
        note.save(update_fields={"label"})
    with owner_schema_editor() as editor:
        editor.execute(f"ALTER TABLE skeleton.{PROBE} DROP COLUMN extra")
        editor.execute(f"ALTER TABLE skeleton.{PROBE} ALTER COLUMN note SET NOT NULL")
    sync_table_catalog()
    _, changed = _catalog()
    assert changed[PROBE, "extra"][:3] == (fields[PROBE, "extra"][0], fields[PROBE, "extra"][1] + 1, False)
    assert changed[PROBE, "note"][0] == fields[PROBE, "note"][0] and changed[PROBE, "note"][3] == "Human note"
    with system_context(reason="test:catalog"):
        assert FieldDefinition.all_objects.get(pk=note.pk).nullable is False
    assert changed[PROBE, "id"] == fields[PROBE, "id"], "An unchanged column is not written."
    settled = _catalog()
    sync_table_catalog()
    assert _catalog() == settled

    # A removed table and its still available columns are marked, never deleted.
    with owner_schema_editor() as editor:
        editor.execute(f"DROP TABLE skeleton.{PROBE}")
    sync_table_catalog()
    tables, removed = _catalog()
    assert tables[PROBE][0] == table_id and tables[PROBE][2] is False
    assert removed[PROBE, "id"][2] is False and removed[PROBE, "note"][2] is False
    assert removed[PROBE, "extra"] == settled[1][PROBE, "extra"], "Already unavailable: not written again."
    assert removed[PROBE, "note"][3] == "Human note"
    gone = _catalog()
    sync_table_catalog()
    assert _catalog() == gone

    # The same table again: the same rows become available, with the same identifiers.
    with owner_schema_editor() as editor:
        editor.execute(f"CREATE TABLE skeleton.{PROBE} (id integer PRIMARY KEY, note text)")
        editor.execute(f"GRANT SELECT ON skeleton.{PROBE} TO skeleton_runtime")
    sync_table_catalog()
    tables, back = _catalog()
    assert tables[PROBE][0] == table_id and tables[PROBE][2] is True
    assert back[PROBE, "id"][0] == fields[PROBE, "id"][0] and back[PROBE, "id"][2] is True
    assert back[PROBE, "note"][2] is True and back[PROBE, "extra"][2] is False
    assert {name: row for name, row in tables.items() if name != PROBE} == untouched_tables


OPERATIONAL = {
    "sk_events_domainevent",
    "sk_files_storageusage",
    "sk_integrations_apitokenactivity",
    "sk_integrations_webhookdelivery",
    "sk_integrations_webhookendpointhealth",
    "sk_jobs_job",
    "sk_jobs_workerheartbeat",
    "sk_notifications_notification",
    "sk_notifications_notificationdelivery",
    "sk_notifications_notificationrecipient",
}


def test_catalog_written_by_migrate_is_the_runtime_catalog(skeleton_db):
    """post_migrate sees historical models, without ``lifecycle`` or the app's default auto field (#349)."""
    from django.apps import apps

    from skeleton.system.models import FieldDefinition, TableDefinition
    from skeleton.system.synchronization import sync_table_catalog
    from skeleton.tenancy.context import system_context

    # The schema of this test was migrated once and restored: the catalog is the one migrate wrote.
    with system_context(reason="test:catalog"):
        rows = {row.db_table: row for row in TableDefinition.all_objects.all()}
        group_permissions = FieldDefinition.all_objects.get(table__db_table="auth_group_permissions", column="id")
    operational = {
        model._meta.db_table for model in apps.get_models() if getattr(model, "lifecycle", None) == "operational"
    }
    assert operational >= OPERATIONAL, "The list above names the operational tables this test guards."
    for name in operational:
        row = rows[name]
        assert (row.base_class, row.soft_delete, row.audit_mode) == ("OperationalRecord", False, "operational"), name
    assert group_permissions.type_name == "AutoField"

    with CaptureQueriesContext(connection) as queries:
        sync_table_catalog()
    writes = [query["sql"] for query in queries if query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE"))]
    assert writes == [], f"{len(writes)} catalog rows differed from what migrate wrote."
