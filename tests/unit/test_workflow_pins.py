"""Immutable references in CI and builds, and versions repeated by hand kept equal (#309)."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted(
    [
        *(ROOT / ".github" / "workflows").glob("*.yml"),
        *(ROOT / ".claude" / "skills" / "dev-workflow" / "assets" / "workflows").glob("*.yml"),
    ]
)
USES = re.compile(r"^\s*-?\s*uses:\s*(\S+)(.*)$")
IMAGE = re.compile(r"python:3\.14\.\d+-slim@sha256:[0-9a-f]{64}")


def uses_lines():
    for path in WORKFLOWS:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = USES.match(line)
            if match:
                yield path, number, match.group(1), match.group(2).strip()


def test_every_action_is_pinned_by_commit_sha():
    found = list(uses_lines())
    assert found
    for path, number, reference, comment in found:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", reference), f"{path.name}:{number} {reference}"
        assert re.fullmatch(r"# v\d+(\.\d+)*", comment), f"{path.name}:{number} needs '# v<tag>'"


def test_same_action_uses_the_same_sha_everywhere():
    seen = {}
    for _path, _number, reference, comment in uses_lines():
        action, sha = reference.split("@")
        seen.setdefault(action, set()).add((sha, comment))
    assert {action: values for action, values in seen.items() if len(values) > 1} == {}


def test_python_base_image_is_pinned_by_digest_and_consistent():
    references = []
    for path in (ROOT / "Dockerfile", ROOT / "deploy" / "agent" / "Dockerfile"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if "python:" in line and (line.startswith("FROM") or line.startswith("ARG RELEASE_BASE=")):
                image = IMAGE.search(line)
                assert image, (path.name, line)
                references.append(image.group(0))
    assert len(references) == 3 and len(set(references)) == 1, references


def text(*parts):
    return ROOT.joinpath(*parts).read_text(encoding="utf-8")


def action_versions(workflow, action):
    """The ``version:`` input given to every use of ``action`` in a workflow."""
    pattern = rf'{action}@\S+ # v[\d.]+\n\s+with:\n(?:\s+#.*\n)*\s+version: "([\d.]+)"'
    return set(re.findall(pattern, workflow))


def test_ruff_and_uv_versions_are_consistent():
    ci, locks = text(".github", "workflows", "ci.yml"), text(".github", "workflows", "dependabot-locks.yml")
    ruff = {
        "ci.yml": action_versions(ci, "astral-sh/ruff-action"),
        ".pre-commit-config.yaml": set(
            re.findall(r"ruff-pre-commit\n\s+rev: v([\d.]+)", text(".pre-commit-config.yaml"))
        ),
        "README.md": set(re.findall(r"ruff@([\d.]+)", text("README.md"))),
    }
    assert all(ruff.values()) and len(set().union(*ruff.values())) == 1, ruff
    uv = {
        "ci.yml": action_versions(ci, "astral-sh/setup-uv"),
        "dependabot-locks.yml": action_versions(locks, "astral-sh/setup-uv"),
        "README.md": set(re.findall(r"\buv (\d+\.\d+\.\d+)", text("README.md"))),
    }
    assert all(uv.values()) and len(set().union(*uv.values())) == 1, uv


def test_agent_runtime_pins_match_the_lock():
    agent = tomllib.loads((ROOT / "deployment_agent" / "pyproject.toml").read_text(encoding="utf-8"))
    core = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    dev_lock = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    for requirement in agent["project"]["dependencies"]:
        name, version = requirement.split("==")
        if name == "django-skeleton":
            assert version == core["project"]["version"]
        else:
            assert re.search(rf"^{re.escape(name)}=={re.escape(version)} ", lock, re.MULTILINE), requirement
    for requirement in agent["build-system"]["requires"]:
        name, version = requirement.split("==")
        assert re.search(rf"^{re.escape(name)}=={re.escape(version)} ", dev_lock, re.MULTILINE), requirement
