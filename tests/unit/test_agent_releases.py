"""Hash-locked requirements, project copy without links, drift detection and frozen releases (#247)."""

import os
import stat

import pytest

pytest.importorskip("fcntl", reason="O agente de deployment exige fcntl (Linux ou macOS).")

from packaging.markers import default_environment  # noqa: E402

from deployment_agent.releases import (  # noqa: E402
    check_drift,
    copy_project,
    freeze,
    owned_snapshot,
    parse_lock,
    remove_release,
)
from deployment_agent.storage import AgentError, inventory_files  # noqa: E402

pytestmark = pytest.mark.posix
HASH = "a" * 64
OTHER_HASH = "b" * 64


def lock(tmp_path, text):
    path = tmp_path / "requirements.lock"
    path.write_text(text)
    return path


def test_parse_lock_accepts_pinned_hashed_requirement(tmp_path):
    text = (
        "# Generated lock\n"
        f"Django==6.1.3 --hash=sha256:{HASH} \\\n    --hash=sha256:{OTHER_HASH}\n"
        f"psycopg[binary]==3.3.0 --hash=sha256:{HASH}  # comment\n"
    )
    rows = parse_lock(lock(tmp_path, text))
    assert rows == {
        "django": {"version": "6.1.3", "hashes": {HASH, OTHER_HASH}, "extras": set()},
        "psycopg": {"version": "3.3.0", "hashes": {HASH}, "extras": {"binary"}},
    }


@pytest.mark.parametrize(
    "line",
    [
        f"django>=6.1 --hash=sha256:{HASH}",
        f"django~=6.1 --hash=sha256:{HASH}",
        f"django==6.* --hash=sha256:{HASH}",
        f"django==6.1.3,<7 --hash=sha256:{HASH}",
        f"django @ https://example.test/django-6.1.3.whl --hash=sha256:{HASH}",
        f"django --hash=sha256:{HASH}",
        "django==6.1.3",
    ],
)
def test_parse_lock_refuses_unpinned_range_wildcard_url_and_unhashed(tmp_path, line):
    with pytest.raises(AgentError, match="unlocked_dependency"):
        parse_lock(lock(tmp_path, line + "\n"))


@pytest.mark.parametrize(
    "line",
    [
        f"not a requirement !!! --hash=sha256:{HASH}",
        "django==6.1.3 --hash=sha256:short",  # A malformed hash is not stripped and breaks the requirement.
        "django==6.1.3 --hash=md5:" + "c" * 32,
    ],
)
def test_parse_lock_refuses_unparseable_requirement(tmp_path, line):
    with pytest.raises(AgentError, match="invalid_requirements_lock"):
        parse_lock(lock(tmp_path, line + "\n"))


def test_parse_lock_refuses_duplicate_package(tmp_path):
    text = f"Django==6.1.3 --hash=sha256:{HASH}\ndjango==6.1.3 --hash=sha256:{OTHER_HASH}\n"
    with pytest.raises(AgentError, match="duplicate_locked_dependency"):
        parse_lock(lock(tmp_path, text))


def test_parse_lock_skips_requirement_whose_marker_does_not_match(tmp_path):
    text = (
        f'pywin32==306 ; sys_platform == "win32" --hash=sha256:{HASH}\n'
        f'uvloop==0.21.0 ; sys_platform == "linux" --hash=sha256:{HASH}\n'
    )
    linux = {**default_environment(), "sys_platform": "linux"}
    assert set(parse_lock(lock(tmp_path, text), linux)) == {"uvloop"}
    windows = {**default_environment(), "sys_platform": "win32"}
    assert set(parse_lock(lock(tmp_path, text), windows)) == {"pywin32"}
    # A skipped row is still checked for pinning and hashes.
    with pytest.raises(AgentError, match="unlocked_dependency"):
        parse_lock(lock(tmp_path, 'pywin32>=300 ; sys_platform == "win32"\n'), linux)


def project(tmp_path):
    source = tmp_path / "project"
    (source / "config").mkdir(parents=True)
    (source / "config" / "settings.py").write_text("SETTING = 1\n")
    (source / "manage.py").write_text("print('manage')\n")
    return source


def test_copy_project_refuses_symlinked_path_and_symlink_inside_tree(tmp_path):
    source = project(tmp_path)
    (source / "linked").symlink_to(source / "config")
    with pytest.raises(AgentError, match="project_path_missing_or_link"):
        copy_project({"project_paths": ["linked"]}, source, tmp_path / "r1", managed_packages=set())
    with pytest.raises(AgentError, match="project_path_missing_or_link"):
        copy_project({"project_paths": ["missing"]}, source, tmp_path / "r2", managed_packages=set())
    (source / "config" / "escape.py").symlink_to(tmp_path / "outside.py")
    with pytest.raises(AgentError, match="project_symlink"):
        copy_project({"project_paths": ["config"]}, source, tmp_path / "r3", managed_packages=set())
    assert not (tmp_path / "r3" / "config").exists()


def test_copy_project_skips_env_files_sqlite_and_venv(tmp_path):
    source = project(tmp_path)
    config = source / "config"
    for relative in (
        ".env",
        ".private/key",
        ".venv/bin/python",
        "db.sqlite3",
        "node_modules/x.js",
        "__pycache__/a.pyc",
    ):
        (config / relative).parent.mkdir(parents=True, exist_ok=True)
        (config / relative).write_text("secret")
    (config / "compiled.pyc").write_text("bytecode")
    release = tmp_path / "release"
    release.mkdir()
    copy_project({"project_paths": ["config", "manage.py"]}, source, release, managed_packages=set())
    copied = sorted(path.relative_to(release).as_posix() for path in release.rglob("*") if path.is_file())
    assert copied == ["applications/__init__.py", "config/settings.py", "manage.py"]
    for name in ("archives", "retained", "wheels", "staticfiles"):
        assert (release / name).is_dir()


def test_copy_project_does_not_copy_managed_app_packages(tmp_path):
    source = project(tmp_path)
    for package in ("notes", "local"):
        (source / "applications" / package).mkdir(parents=True)
        (source / "applications" / package / "__init__.py").write_text("")
    release = tmp_path / "release"
    release.mkdir()
    copy_project({"project_paths": ["applications"]}, source, release, managed_packages={"notes"})
    assert (release / "applications" / "local" / "__init__.py").is_file()
    assert not (release / "applications" / "notes").exists()


def snapshot_release(tmp_path):
    release = tmp_path / "release"
    (release / "config").mkdir(parents=True)
    (release / "config" / "settings.py").write_text("SETTING = 1\n")
    (release / "manage.py").write_text("")
    return release, owned_snapshot(release)


def test_check_drift_detects_changed_added_and_removed_files(tmp_path):
    release, expected = snapshot_release(tmp_path)
    check_drift(release, expected)
    changes = (
        lambda: (release / "config" / "settings.py").write_text("SETTING = 2\n"),
        lambda: (release / "config" / "extra.py").write_text(""),
        lambda: (release / "manage.py").unlink(),
        lambda: (release / "config" / "linked.py").symlink_to(release / "manage.py"),
    )
    for change in changes:
        release, expected = snapshot_release(tmp_path / str(id(change)))
        change()
        with pytest.raises(AgentError, match="local_file_drift"):
            check_drift(release, expected)


def test_check_drift_reads_owned_files_json_by_default(tmp_path):
    from deployment_agent.storage import atomic_json

    release, expected = snapshot_release(tmp_path)
    atomic_json(release / "owned-files.json", expected)
    check_drift(release)  # owned-files.json itself is not part of the snapshot.
    (release / "manage.py").write_text("changed")
    with pytest.raises(AgentError, match="local_file_drift"):
        check_drift(release)


def test_check_drift_ignores_pycache(tmp_path):
    release, expected = snapshot_release(tmp_path)
    (release / "config" / "__pycache__").mkdir()
    (release / "config" / "__pycache__" / "settings.cpython-314.pyc").write_bytes(b"\x00")
    (release / "config" / "stale.pyc").write_bytes(b"\x00")
    check_drift(release, expected)


def test_owned_snapshot_classifies_owners(tmp_path):
    release = tmp_path / "release"
    files = {
        "applications/notes/models.py": "app:notes",
        ".venv/lib/site.py": "environment",
        "staticfiles/app.css": "deployment",
        "archives/notes/0001.py": "deployment",
        "retained/notes.json": "deployment",
        "wheels/x.whl": "deployment",
        "inventory.json": "deployment",
        "requirements.lock": "deployment",
        "manage.py": "project",
        "config/settings.py": "project",
    }
    for relative in [*files, "owned-files.json"]:
        (release / relative).parent.mkdir(parents=True, exist_ok=True)
        (release / relative).write_text(relative)
    snapshot = owned_snapshot(release)
    assert {path: row["owner"] for path, row in snapshot.items()} == files
    assert snapshot["manage.py"]["type"] == "file" and len(snapshot["manage.py"]["sha256"]) == 64


def test_freeze_makes_release_read_only(tmp_path):
    release = tmp_path / "release"
    (release / "config").mkdir(parents=True)
    (release / "config" / "settings.py").write_text("")
    (release / "bin").mkdir()
    (release / "bin" / "run").write_text("#!/bin/sh\n")
    os.chmod(release / "bin" / "run", 0o755)
    freeze(release)
    try:
        assert stat.S_IMODE(release.stat().st_mode) == 0o555
        assert stat.S_IMODE((release / "config").stat().st_mode) == 0o555
        assert stat.S_IMODE((release / "config" / "settings.py").stat().st_mode) == 0o444
        assert stat.S_IMODE((release / "bin" / "run").stat().st_mode) == 0o555
        if os.geteuid() != 0:  # root ignores the mode bits.
            with pytest.raises(PermissionError):
                (release / "config" / "settings.py").write_text("changed")
            with pytest.raises(PermissionError):
                (release / "config" / "new.py").write_text("")
    finally:
        remove_release(release)
    assert not release.exists()


def test_remove_release_refuses_a_symlink(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    with pytest.raises(AgentError, match="release_path_is_symlink"):
        remove_release(tmp_path / "link")
    assert (tmp_path / "real").is_dir()


def test_inventory_files_refuses_special_files(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    (release / "file.py").write_text("")
    assert set(inventory_files(release)) == {"file.py"}
    os.mkfifo(release / "pipe")
    with pytest.raises(AgentError, match="unsupported_file_type"):
        inventory_files(release)


def test_inventory_files_records_links_without_following_them(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    (tmp_path / "outside").write_text("secret")
    (release / "link").symlink_to(tmp_path / "outside")
    row = inventory_files(release)["link"]
    assert row["type"] == "symlink" and "size" not in row
