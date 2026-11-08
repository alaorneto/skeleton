import importlib
from io import StringIO

import pytest
from django.apps import apps
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test import override_settings

from skeleton.common.migration_operations import (
    InstallRecordInfrastructure,
    build_infrastructure_specification,
    infrastructure_names,
)
from skeleton.system.registry import get_spec

pytestmark = pytest.mark.skipif(
    not apps.is_installed("examples.notes"), reason="Requer SKELETON_ENABLE_EXAMPLE_APP=true."
)


def _notes_frozen_operation():
    migration = importlib.import_module("examples.notes.migrations.0001_initial").Migration
    (operation,) = [item for item in migration.operations if isinstance(item, InstallRecordInfrastructure)]
    return operation


def test_generated_specification_matches_frozen_notes_migration():
    spec = get_spec("example_notes.note")
    assert build_infrastructure_specification(spec.model, spec) == _notes_frozen_operation().specification


def test_command_reports_nothing_for_notes():
    out = StringIO()
    call_command("record_infrastructure", "example_notes", stdout=out)
    assert out.getvalue().strip() == "No pending record infrastructure for example_notes."


def test_command_rejects_unknown_app_and_core_write():
    with pytest.raises(CommandError, match="Unknown app label"):
        call_command("record_infrastructure", "missing_app")
    # Core infrastructure is complete; nothing to write and therefore no error.
    call_command("record_infrastructure", "sk_tenancy", "--write", stdout=StringIO())


def test_write_creates_new_valid_migration_for_model_without_infrastructure(skeleton_db, tmp_path, monkeypatch):
    package = tmp_path / "sk_record_infra_probe"
    package.mkdir()
    (package / "__init__.py").write_text("")
    # Same schema as the Notes migration, but without its infrastructure operation.
    initial = package / "0001_initial.py"
    initial.write_text(
        "from importlib import import_module\n"
        "from django.db import migrations\n"
        "from skeleton.common.migration_operations import InstallRecordInfrastructure\n"
        "_original = import_module('examples.notes.migrations.0001_initial').Migration\n"
        "class Migration(migrations.Migration):\n"
        "    initial = True\n"
        "    dependencies = _original.dependencies\n"
        "    operations = [op for op in _original.operations if not isinstance(op, InstallRecordInfrastructure)]\n"
    )
    original_initial = initial.read_bytes()
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    with override_settings(MIGRATION_MODULES={"example_notes": "sk_record_infra_probe"}):
        out = StringIO()
        call_command("record_infrastructure", "example_notes", stdout=out)
        assert out.getvalue().strip() == "example_notes.note: missing"
        assert sorted(path.name for path in package.glob("0*.py")) == ["0001_initial.py"]

        call_command("record_infrastructure", "example_notes", "--write", stdout=StringIO())
        assert initial.read_bytes() == original_initial
        created = package / "0002_record_infrastructure.py"
        assert created.exists()

        loader = MigrationLoader(None)
        migration = loader.disk_migrations[("example_notes", "0002_record_infrastructure")]
        assert sorted(migration.dependencies) == [
            ("example_notes", "0001_initial"),
            ("sk_audit", "0002_record_infrastructure"),
        ]
        (operation,) = migration.operations
        assert isinstance(operation, InstallRecordInfrastructure)
        assert (operation.app_label, operation.model_name) == ("example_notes", "note")
        assert operation.specification == _notes_frozen_operation().specification
        # Applied with historical states on a real SQLite schema, it reinstalls the exact triggers.
        before = loader.project_state(("example_notes", "0001_initial"))
        after = loader.project_state(("example_notes", "0002_record_infrastructure"))
        table = after.apps.get_model("example_notes", "note")._meta.db_table
        names = set(infrastructure_names(table, "sqlite"))
        with connection.cursor() as cursor:
            cursor.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name=%s", [table])
            original = dict(cursor.fetchall())
            assert set(original) == names
            for name in names:
                cursor.execute(f"DROP TRIGGER {connection.ops.quote_name(name)}")
        with connection.schema_editor() as editor:
            operation.database_forwards("example_notes", editor, before, after)
        with connection.cursor() as cursor:
            cursor.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name=%s", [table])
            assert dict(cursor.fetchall()) == original

        out = StringIO()
        call_command("record_infrastructure", "example_notes", stdout=out)
        assert out.getvalue().strip() == "No pending record infrastructure for example_notes."
        call_command("record_infrastructure", "example_notes", "--write", stdout=StringIO())
        assert sorted(path.name for path in package.glob("0*.py")) == [
            "0001_initial.py",
            "0002_record_infrastructure.py",
        ]
