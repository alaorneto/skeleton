"""Private files of the checkout never reach the Docker build context or its layers (#248)."""

import re
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


def test_gitignore_keeps_sso_fixtures_out_of_the_repository():
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".sso-fixtures/" in lines and ".private/" in lines
