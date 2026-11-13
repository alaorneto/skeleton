"""package doctor: every problem of an app source reported at once, without building."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

from skeleton.system.packages import canonical_json, doctor, load_manifest

ROOT = Path(__file__).resolve().parents[2]
NOTES = ROOT / "examples" / "notes"


def notes_copy(tmp_path):
    target = tmp_path / "notes"
    shutil.copytree(NOTES, target, ignore=shutil.ignore_patterns("__pycache__"))
    return target


def codes(result):
    return [problem["code"] for problem in result["problems"]]


def test_example_application_is_healthy():
    result = doctor(NOTES)
    assert result == {"ok": True, "app_id": "org.skeleton.example-notes", "problems": []}


def test_reports_all_source_problems_together(tmp_path):
    root = notes_copy(tmp_path)
    (root / "docs" / "adr.md").write_text("## Context\nOnly context.\n")
    (root / "docs" / "spec.md").unlink()
    (root / "secret.pem").write_text("private")
    (root / "migrations" / "0002_extra.py").write_text(
        "from django.db import migrations\n\n"
        "class Migration(migrations.Migration):\n"
        "    dependencies = [('example_notes', '0001_initial')]\n"
        "    operations = []\n"
    )
    result = doctor(root)
    assert not result["ok"]
    assert sorted(codes(result)) == [
        "documentation_missing",
        "documentation_sections",
        "migration_leaves_mismatch",
        "private_file",
    ]


def test_invalid_manifest_stops_with_one_problem(tmp_path):
    root = notes_copy(tmp_path)
    (root / "manifest.json").write_text("{}")
    assert codes(doctor(root)) == ["manifest_invalid"]


def inventory_for(manifests):
    return {
        "generation": 1,
        "core": {"version": "0.4.0"},
        "runtime": {"python": "3.14.0", "django": "6.1.3"},
        "apps": [{"manifest": manifest, "state": "installed"} for manifest in manifests],
    }


def test_dependencies_resolve_against_the_target_inventory(tmp_path):
    root = notes_copy(tmp_path)
    manifest = load_manifest(root / "manifest.json")
    manifest["dependencies"] = {"org.test.base": ">=1.0"}
    (root / "manifest.json").write_bytes(canonical_json(manifest))
    result = doctor(root, inventory=inventory_for([]))
    assert result["problems"] == [
        {
            "code": "missing_dependency",
            "message": "missing_dependency: org.skeleton.example-notes -> org.test.base >=1.0",
            "component": "org.skeleton.example-notes",
            "dependency": "org.test.base",
            "required": ">=1.0",
        }
    ]


def test_cli_exits_non_zero_and_prints_json(tmp_path):
    root = notes_copy(tmp_path)
    (root / "docs" / "spec.md").unlink()
    completed = subprocess.run(
        [sys.executable, "-m", "skeleton.system.packages", "doctor", str(root)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 1
    assert codes(json.loads(completed.stdout)) == ["documentation_missing"]


def test_doctor_lists_import_boundary_violations_before_build(tmp_path):
    root = notes_copy(tmp_path)
    (root / "integration.py").write_text(
        "from applications.tasks.models import Task\n"
        "from applications.tasks.service_api import TaskService\n"
        "from skeleton.access.services import provision_membership\n"
    )
    tasks = {
        "app_id": "com.example.tasks",
        "package_name": "tasks",
        "runtime": {"service_api": {"module": "applications.tasks.service_api", "classes": ["TaskService"]}},
    }
    result = doctor(root, inventory={"apps": [{"manifest": tasks, "state": "installed"}]})
    boundary = [
        p for p in result["problems"] if p["code"] in {"internal_import", "undeclared_dependency", "core_import"}
    ]
    assert [(p["code"], p["line"], p.get("severity")) for p in boundary] == [
        ("internal_import", 1, None),
        ("undeclared_dependency", 2, None),
        ("core_import", 3, "warning"),
    ]
    assert not result["ok"]
    (root / "integration.py").write_text("from skeleton.access.services import provision_membership\n")
    warning_only = doctor(root)
    assert warning_only["ok"] and codes(warning_only) == ["core_import"]


def test_doctor_warns_that_default_enabled_is_obsolete(tmp_path):
    root = notes_copy(tmp_path)
    manifest = load_manifest(root / "manifest.json")
    assert manifest["runtime"]["default_enabled"] is False
    assert codes(doctor(root)) == []
    manifest["runtime"]["default_enabled"] = True
    (root / "manifest.json").write_bytes(canonical_json(manifest))
    result = doctor(root)
    # A warning only: signed v2 packages that still say true keep building and installing.
    assert result["ok"] is True
    assert [(problem["code"], problem["severity"]) for problem in result["problems"]] == [
        ("default_enabled_ignored", "warning")
    ]


def test_doctor_accepts_dependency_on_installed_first_party_app(tmp_path):
    """Regression (#254): --bundled-app satisfies a dependency on a first-party app."""
    root = notes_copy(tmp_path)
    manifest = load_manifest(root / "manifest.json")
    manifest["dependencies"] = {"org.skeleton.calendar": ">=0.4.0"}
    (root / "manifest.json").write_bytes(canonical_json(manifest))
    without = doctor(root, inventory=inventory_for([]))
    assert codes(without) == ["missing_dependency"]
    assert doctor(root, inventory=inventory_for([]), bundled_apps=["calendar"])["problems"] == []
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "skeleton.system.packages",
            "doctor",
            str(root),
            "--inventory",
            str(_write(tmp_path / "inventory.json", inventory_for([]))),
            "--bundled-app",
            "calendar",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout)["ok"] is True


def _write(path, data):
    path.write_text(json.dumps(data))
    return path


def test_doctor_reports_unknown_bundled_app(tmp_path):
    result = doctor(notes_copy(tmp_path), bundled_apps=["inexistente"])
    assert codes(result) == ["bundled_app_unknown"]
    assert "'inexistente'" in result["problems"][0]["message"]
