"""From skeleton_startapp to a healthy, packageable app, using the real commands in a scratch project."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LABEL = "project_tasks"


@pytest.fixture
def project(tmp_path, skeleton_db):
    from tools.integration_database import disposable_database_pair

    # The scratch project migrates the disposable schema as the owner (the role that migrates);
    # the next test restores the schema template, so nothing of it survives this test.
    owner = disposable_database_pair().owner
    target = tmp_path / "project"
    shutil.copytree(ROOT / "config", target / "config", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(ROOT / "manage.py", target / "manage.py")
    (target / "applications").mkdir()
    (target / "applications" / "__init__.py").write_text("")
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(target), str(ROOT)]),
        "DJANGO_SETTINGS_MODULE": "config.settings.test",
        "SKELETON_PROJECT_ROOT": str(target),
        "TEST_DATABASE_URL": owner,
        # Same apps as the schema template this test restored (an unknown table would be unclassified).
        "SKELETON_ENABLE_EXAMPLE_APP": os.environ.get("SKELETON_ENABLE_EXAMPLE_APP", "false"),
    }
    environment.pop("SKELETON_DEPLOYMENT_INVENTORY", None)

    def run(*arguments, check=True):
        completed = subprocess.run(
            [sys.executable, "manage.py", *arguments], cwd=target, env=environment, capture_output=True, text=True
        )
        if check and completed.returncode != 0:
            pytest.fail(f"{' '.join(arguments)} failed:\n{completed.stdout}\n{completed.stderr}")
        return completed

    run.environment = environment
    return target, run


def test_scaffolded_app_migrates_passes_checks_and_doctor(project):
    target, run = project
    created = run(
        "skeleton_startapp",
        LABEL,
        "--app-id",
        "com.example.tasks",
        "--publisher",
        "example-publisher",
        "--model",
        "Task",
        "--package-name",
        "tasks",
    )
    assert "record_infrastructure project_tasks --write" in created.stdout
    (target / "config" / "settings" / "project.py").write_text(
        'PROJECT_APPS = ["applications.tasks.apps.TasksConfig"]\n'
    )
    run("makemigrations", LABEL)
    pending = run("record_infrastructure", LABEL)
    assert "project_tasks.task: missing" in pending.stdout
    run("record_infrastructure", LABEL, "--write")
    assert "No pending record infrastructure" in run("record_infrastructure", LABEL).stdout

    # The manifest still declares no leaves: the check warns without blocking, and doctor refuses.
    assert "skeleton.W017" in run("check").stderr
    stale = json.loads(run("package", "doctor", "applications/tasks", check=False).stdout)
    assert [problem["code"] for problem in stale["problems"]] == ["migration_leaves_mismatch"]
    manifest_path = target / "applications" / "tasks" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["migrations"]["leaves"] = ["0002_record_infrastructure"]
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

    run("check")
    run("migrate", "--noinput")
    run("sync_table_catalog")
    run("check_skeleton_schema")
    report = json.loads(run("package", "doctor", "applications/tasks").stdout)
    assert report == {"ok": True, "app_id": "com.example.tasks", "problems": []}

    # The generated service API passes E018/E019 with warnings as errors and its own contract test.
    checked = run("check", "--fail-level", "WARNING")
    assert "skeleton.E018" not in checked.stderr and "skeleton.E019" not in checked.stderr
    contract = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "applications/tasks/tests"],
        cwd=target,
        env=run.environment,
        capture_output=True,
        text=True,
    )
    assert contract.returncode == 0, contract.stdout + contract.stderr
    assert "1 passed" in contract.stdout
