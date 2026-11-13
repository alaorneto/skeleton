"""export_infrastructure_inventory and export_record_schema write private JSON once (#335)."""

import io
import json
import stat

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

pytestmark = pytest.mark.usefixtures("skeleton_db")


def export(command, *args):
    out = io.StringIO()
    call_command(command, *args, stdout=out)
    return out.getvalue().strip()


def test_export_infrastructure_inventory_writes_json(tmp_path):
    path = tmp_path / "inv.json"
    assert export("export_infrastructure_inventory", "--output", str(path)) == str(path)
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert json.loads(text)


@pytest.mark.posix
def test_export_infrastructure_inventory_is_private(tmp_path):
    path = tmp_path / "inv.json"
    export("export_infrastructure_inventory", "--output", str(path))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_export_refuses_to_overwrite(tmp_path):
    path = tmp_path / "inv.json"
    path.write_text("previous\n", encoding="utf-8")
    with pytest.raises(CommandError) as raised:
        export("export_infrastructure_inventory", "--output", str(path))
    assert str(raised.value) == "Infrastructure export failed: FileExistsError"
    assert path.read_text(encoding="utf-8") == "previous\n"


@pytest.mark.needs_example_app
def test_export_record_schema_for_the_example_app(tmp_path):
    path = tmp_path / "notes.json"
    assert export("export_record_schema", "--app-label", "example_notes", "--output", str(path)) == str(path)
    assert json.loads(path.read_text(encoding="utf-8"))
    # A well-formed label without installed tables is refused too, without writing the file (#366).
    with pytest.raises(CommandError) as missing:
        export("export_record_schema", "--app-label", "missing_app", "--output", str(tmp_path / "missing.json"))
    assert str(missing.value) == "Schema export failed: RetainedSchemaError"
    assert not (tmp_path / "missing.json").exists()
    with pytest.raises(CommandError) as raised:
        export("export_record_schema", "--app-label", "Not a label!", "--output", str(tmp_path / "bad.json"))
    message = str(raised.value)
    assert message.startswith("Schema export failed: ")
    assert message == "Schema export failed: RetainedSchemaError"
    assert not (tmp_path / "bad.json").exists()
