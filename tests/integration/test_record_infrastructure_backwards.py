"""Reverting a table's infrastructure leaves it closed to the runtime role (#306)."""

import importlib
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.db import connection, models
from django.test.utils import isolate_apps

from skeleton.common.migration_operations import InstallRecordInfrastructure
from skeleton.common.models import OrganizationRecord

pytestmark = pytest.mark.usefixtures("skeleton_db")
TABLE = "sk_test_backwards"
SPEC = {"scope": "organization", "sensitive_fields": []}


def spec(model):
    """The frozen specification of the probe: every concrete column, none sensitive."""
    columns = [
        {"name": f.column, "type": f.get_internal_type(), "sensitive": False} for f in model._meta.concrete_fields
    ]
    return {**SPEC, "columns": columns}


@pytest.fixture
def probe():
    with isolate_apps("skeleton.system.apps.SystemConfig"):

        class BackwardsProbe(OrganizationRecord):
            # Resolved classes: the isolated registry does not know the core apps by label.
            organization = models.ForeignKey(
                apps.get_model("sk_tenancy", "Organization"), on_delete=models.PROTECT, related_name="+"
            )
            role = models.ForeignKey(apps.get_model("sk_access", "Role"), on_delete=models.PROTECT, related_name="+")

            class Meta(OrganizationRecord.Meta):
                app_label = "sk_system"
                db_table = TABLE

        yield BackwardsProbe


class Capture:
    def __init__(self):
        self.statements = []
        self.connection = connection

    def quote_name(self, name):
        return connection.ops.quote_name(name)

    def execute(self, sql, params=None):
        self.statements.append(str(sql))


def privileges():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT has_table_privilege(current_user, %s, 'SELECT'), has_table_privilege(current_user, %s, 'INSERT'),"
            " has_table_privilege(current_user, %s, 'UPDATE'), has_table_privilege(current_user, %s, 'DELETE')",
            [f"skeleton.{TABLE}"] * 4,
        )
        return cursor.fetchone()


def catalog():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relrowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace"
            " WHERE n.nspname='skeleton' AND c.relname=%s",
            [TABLE],
        )
        rls = cursor.fetchone()[0]
        cursor.execute(
            "SELECT count(*) FROM pg_indexes WHERE schemaname='skeleton' AND indexname LIKE %s", [TABLE + "_sk_idx_%"]
        )
        indexes = cursor.fetchone()[0]
        cursor.execute(
            "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid WHERE c.relname=%s AND NOT t.tgisinternal",
            [TABLE],
        )
        return rls, indexes, cursor.fetchone()[0]


def test_backwards_revokes_runtime_privileges_and_drops_indexes(probe, owner_schema_editor):
    operation = InstallRecordInfrastructure("sk_system", "backwardsprobe", spec(probe))
    with owner_schema_editor() as editor:
        editor.create_model(probe)
        operation._postgres(editor, probe)
    assert privileges()[0] is True
    assert catalog() == (True, 1, 2)
    with owner_schema_editor() as editor:
        operation._postgres_backwards(editor, probe)
    assert privileges() == (False, False, False, False)
    assert catalog() == (False, 0, 0)
    with owner_schema_editor() as editor:
        operation._postgres(editor, probe)
    assert privileges() == (True, True, True, False)
    assert catalog() == (True, 1, 2)


def test_backwards_statement_order_starts_with_revoke(probe):
    capture = Capture()
    InstallRecordInfrastructure("sk_system", "backwardsprobe", spec(probe))._postgres_backwards(capture, probe)
    assert capture.statements[0].startswith(f'REVOKE ALL ON skeleton."{TABLE}"')
    assert any(statement.startswith("DROP INDEX IF EXISTS") for statement in capture.statements)


def test_backwards_with_replaces_reinstalls_the_previous_specification():
    from skeleton.tenancy.models import Membership

    migration = importlib.import_module("skeleton.tenancy.migrations.0003_membership_self_discovery")
    state = SimpleNamespace(apps=SimpleNamespace(get_model=lambda app, name: Membership))
    replaced, expected = Capture(), Capture()
    operation = InstallRecordInfrastructure(
        "sk_tenancy",
        "Membership",
        migration.specification(self_discovery_column="user_id"),
        replaces=migration.specification(),
    )
    operation.allow_migrate_model = lambda alias, model: True
    operation.database_backwards("sk_tenancy", replaced, state, state)
    InstallRecordInfrastructure("sk_tenancy", "Membership", migration.specification())._postgres(expected, Membership)
    assert replaced.statements == expected.statements
