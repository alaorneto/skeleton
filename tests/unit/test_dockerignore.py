"""Private files of the checkout never reach the Docker build context or its layers (#248)."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PRIVATE = ("var/", ".private/", ".sso-fixtures/", "**/*.sqlite3", "**/*.pem", "**/*.key")


def ignored():
    return [line.strip() for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines() if line.strip()]


def dockerfile_lines(path):
    """Logical lines (continuations joined), without comments."""
    text = re.sub(r"\\\n", " ", path.read_text(encoding="utf-8"))
    return [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def test_dockerignore_excludes_private_paths_at_any_depth():
    lines = ignored()
    for pattern in PRIVATE:
        assert pattern in lines, pattern


def test_no_stage_copies_the_whole_context():
    for path in (ROOT / "Dockerfile", ROOT / "deploy" / "agent" / "Dockerfile"):
        for line in dockerfile_lines(path):
            assert not re.match(r"(COPY|ADD)\s+(--\S+\s+)*\.\s", line), (path.name, line)


def test_context_is_only_bind_mounted_in_project_assets():
    lines = dockerfile_lines(ROOT / "Dockerfile")
    assert lines[0].startswith("FROM") and (ROOT / "Dockerfile").read_text().startswith("# syntax=docker/dockerfile:1")
    assets = next(index for index, line in enumerate(lines) if line.endswith("AS project-assets"))
    assert "--mount=type=bind,target=/project-source" in lines[assets + 1]


def test_copied_paths_are_not_ignored():
    lines = set(ignored())
    sources = []
    for line in dockerfile_lines(ROOT / "Dockerfile"):
        words = line.split()
        if words[0] != "COPY" or any(word.startswith("--from=") for word in words):
            continue
        sources += [word for word in words[1:-1] if not word.startswith("--")]
    assert {"requirements.txt", "pyproject.toml", "skeleton", "config", "applications", "manage.py"} <= set(sources)
    for source in sources:
        assert source not in lines and source.rstrip("/") + "/" not in lines, source
        assert (ROOT / source).exists(), source


def _pattern(pattern):
    """A .dockerignore pattern as a regex over the path or one of its parent directories."""
    out, index = "", 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            out, index = out + "(?:.*/)?", index + 3
        elif pattern.startswith("**", index):
            out, index = out + ".*", index + 2
        elif pattern[index] == "*":
            out, index = out + "[^/]*", index + 1
        elif pattern[index] == "?":
            out, index = out + "[^/]", index + 1
        else:
            out, index = out + re.escape(pattern[index]), index + 1
    return re.compile(out + "(?:/.*)?")


def excluded(path):
    """Docker's rule: the last pattern that matches decides; ``!`` includes again."""
    result = False
    for line in ignored():
        if line.startswith("#"):
            continue
        negated = line.startswith("!")
        pattern = line[1:] if negated else line
        if _pattern(pattern.strip("/")).fullmatch(path):
            result = not negated
    return result


def copied_files():
    """Every checkout file under a path that a Dockerfile copies from the build context."""
    sources = set()
    for dockerfile in (ROOT / "Dockerfile", ROOT / "deploy" / "agent" / "Dockerfile"):
        for line in dockerfile_lines(dockerfile):
            words = line.split()
            if words[0] in ("COPY", "ADD") and not any(word.startswith("--from=") for word in words):
                sources |= {word for word in words[1:-1] if not word.startswith("--")}
    listed = subprocess.run(  # Versioned files only: local scratch files never matter here.
        ["git", "ls-files", "-z", "--", *sorted(sources)], cwd=ROOT, check=True, capture_output=True
    ).stdout.decode()
    return [path for path in listed.split("\0") if path]


def test_the_publisher_key_is_the_only_pem_that_reaches_the_build_context():
    assert not excluded("deploy/release/publishers/skeleton.pub.pem")  # #720: the agent image needs it.
    for path in ("deploy/release/publishers/skeleton.pem", "deploy/tls/server.pem", "certs/a.pub.pem", "a.key"):
        assert excluded(path), path
    for path in ("var/x", ".private/release.pem", "instance.sqlite3", "deep/dir/db.sqlite3-wal"):
        assert excluded(path), path


def test_every_copied_file_reaches_the_build_context():
    files = copied_files()
    assert "deploy/release/publishers/skeleton.pub.pem" in files
    for path in files:
        if path.endswith((".pem", ".key")) and not path.startswith("deploy/release/publishers/"):
            continue  # Never versioned outside the publishers directory; excluded on purpose.
        assert not excluded(path), path


def test_gitignore_keeps_sso_fixtures_out_of_the_repository():
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".sso-fixtures/" in lines and ".private/" in lines
