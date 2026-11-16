"""package build/doctor --git-ref (#519): the committed tree, honouring export-ignore."""

import io
import json
import os
import shutil
import subprocess
import tarfile
import zipfile

import pytest

from skeleton.system.app_scaffold import build_scaffold, create_application
from skeleton.system.packages import PackageError, export_git_tree, initialize_keys, run_command
from skeleton.system.packaging import gitref

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
# Building verifies the artifact with O_NOFOLLOW, as the other build tests (Linux CI is the proof).
posix_build = pytest.mark.skipif(
    not hasattr(os, "O_NOFOLLOW"), reason="Requer os.O_NOFOLLOW; execute em Linux ou macOS."
)

ATTRIBUTES = ".gitattributes export-ignore\n.gitignore export-ignore\n.github export-ignore\nREADME.md export-ignore\n"


def git(root, *arguments):
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
         *arguments],
        cwd=root, check=True, capture_output=True,
    )  # fmt: skip


@pytest.fixture
def repository(tmp_path):
    scaffold = build_scaffold(app_label="acme_gitref", app_id="com.example.gitref", publisher="example-publisher")
    root, _ = create_application(scaffold, tmp_path / "src")
    (root / ".gitattributes").write_text(ATTRIBUTES, encoding="utf-8")
    (root / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "ci.yml").write_text("on: push\n", encoding="utf-8")
    (root / "README.md").write_text("# acme_gitref\n", encoding="utf-8")
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "initial")
    return root


@pytest.fixture
def keys(tmp_path):
    private, public = tmp_path / "keys" / "private.pem", tmp_path / "keys" / "public.pem"
    private.parent.mkdir()
    initialize_keys(private, public)
    return private


def doctor(root, ref="HEAD"):
    return run_command({"operation": "doctor", "app_root": str(root), "git_ref": ref})


def build(root, output, key, ref="HEAD"):
    return run_command(
        {
            "operation": "build",
            "app_root": str(root),
            "manifest": None,
            "git_ref": ref,
            "output": str(output),
            "private_key": str(key),
            "wheel": [],
        }
    )


def members(path):
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        return {name for name in archive.namelist()}, {entry["path"]: entry["sha256"] for entry in manifest["files"]}


@posix_build
def test_doctor_and_build_use_the_committed_tree(repository, keys, tmp_path):
    report = doctor(repository)
    assert report["ok"] is True, report["problems"]
    assert len(report["commit"]) == 40

    result = build(repository, tmp_path / "out" / "acme.skapp", keys)
    names, files = members(result["artifact"])
    assert result["commit"] == report["commit"]
    assert "app/manifest.json" not in files and "app/apps.py" in files
    assert not any(part.startswith(".") for name in names for part in name.split("/"))
    assert not any(name.endswith("README.md") for name in names)


def test_without_git_ref_the_dotfiles_are_still_refused(repository, tmp_path):
    report = run_command({"operation": "doctor", "app_root": str(repository)})
    assert report["ok"] is False
    assert "private_file" in {problem["code"] for problem in report["problems"]}


def test_a_committed_dotfile_without_export_ignore_is_refused(repository):
    (repository / ".env.example").write_text("X=1\n", encoding="utf-8")
    git(repository, "add", "-A")
    git(repository, "commit", "-q", "-m", "dotfile")
    report = doctor(repository)
    assert report["ok"] is False
    assert {"private_file"} <= {problem["code"] for problem in report["problems"]}


@posix_build
def test_an_uncommitted_change_never_enters_the_package(repository, keys, tmp_path):
    _, before = members(build(repository, tmp_path / "a.skapp", keys)["artifact"])
    (repository / "views.py").write_text("raise SystemExit('uncommitted')\n", encoding="utf-8")
    (repository / "untracked.py").write_text("x = 1\n", encoding="utf-8")
    _, after = members(build(repository, tmp_path / "b.skapp", keys)["artifact"])
    assert before == after


def test_doctor_reports_the_commit_without_building(repository):
    report = doctor(repository)
    assert report["ok"] is True, report["problems"]
    assert len(report["commit"]) == 40


def test_an_uncommitted_change_never_enters_the_export(repository, tmp_path):
    (repository / "views.py").write_text("raise SystemExit('uncommitted')\n", encoding="utf-8")
    (repository / "untracked.py").write_text("x = 1\n", encoding="utf-8")
    export_git_tree(repository, "HEAD", tmp_path / "export")
    assert "uncommitted" not in (tmp_path / "export" / "views.py").read_text(encoding="utf-8")
    assert not (tmp_path / "export" / "untracked.py").exists()
    assert not (tmp_path / "export" / ".gitattributes").exists()


def test_an_app_in_a_subdirectory_of_the_repository_exports_only_its_subtree(repository, tmp_path):
    outer = tmp_path / "outer"
    shutil.copytree(repository, outer / "applications" / "acme_gitref", ignore=shutil.ignore_patterns(".git"))
    (outer / "unrelated.txt").write_text("not the app\n", encoding="utf-8")
    git(outer, "init", "-q")
    git(outer, "add", "-A")
    git(outer, "commit", "-q", "-m", "initial")
    destination = tmp_path / "export"
    export_git_tree(outer / "applications" / "acme_gitref", "HEAD", destination)
    assert (destination / "manifest.json").is_file() and not (destination / "unrelated.txt").exists()


@pytest.mark.parametrize("ref", ["-c", "--output=x", "HEAD;rm", "a b", "HEAD~1", "x" * 201, ""])
def test_invalid_refs_are_refused_before_git_runs(repository, tmp_path, ref):
    with pytest.raises(PackageError, match="Invalid --git-ref"):
        export_git_tree(repository, ref, tmp_path / "export")


def test_an_unknown_ref_is_a_package_error(repository, tmp_path):
    with pytest.raises(PackageError, match="Unknown git revision"):
        export_git_tree(repository, "does-not-exist", tmp_path / "export")


def test_a_directory_outside_a_repository_is_a_package_error(tmp_path):
    (tmp_path / "plain").mkdir()
    with pytest.raises(PackageError, match="not inside a git repository"):
        export_git_tree(tmp_path / "plain", "HEAD", tmp_path / "export")


def test_git_missing_from_path_is_a_package_error(repository, tmp_path, monkeypatch):
    monkeypatch.setattr(gitref.shutil, "which", lambda name: None)
    with pytest.raises(PackageError, match="git is not available"):
        export_git_tree(repository, "HEAD", tmp_path / "export")


def test_the_cli_reports_errors_without_traceback(tmp_path):
    from skeleton.system.packages import main

    (tmp_path / "plain").mkdir()
    with pytest.raises(SystemExit) as error:
        main(["doctor", str(tmp_path / "plain"), "--git-ref", "HEAD"])
    assert error.value.code == 2


def fake_archive(member):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as archive:
        archive.addfile(member, io.BytesIO(b"x") if member.isfile() else None)
    return data.getvalue()


@pytest.mark.parametrize(
    "member",
    [
        tarfile.TarInfo("link"),
        tarfile.TarInfo("hard"),
        tarfile.TarInfo("../escape.txt"),
        tarfile.TarInfo("/absolute.txt"),
    ],
)
def test_links_and_paths_outside_the_destination_are_never_extracted(repository, tmp_path, monkeypatch, member):
    if member.name == "link":
        member.type, member.linkname = tarfile.SYMTYPE, "../../outside"
    elif member.name == "hard":
        member.type, member.linkname = tarfile.LNKTYPE, "manifest.json"
    else:
        member.size = 1
    payload = fake_archive(member)
    real = gitref._git

    def git_with_fake_archive(root, *arguments, stdout=None):
        if arguments[0] == "archive":
            stdout.write(payload)
            return "ok"
        return real(root, *arguments, stdout=stdout)

    monkeypatch.setattr(gitref, "_git", git_with_fake_archive)
    destination = tmp_path / "export"
    with pytest.raises(PackageError):
        export_git_tree(repository, "HEAD", destination)
    assert not (tmp_path / "escape.txt").exists() and not (tmp_path / "outside").exists()
    assert not any(destination.iterdir())
