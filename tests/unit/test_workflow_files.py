"""Workflows that enforce the issue-first flow run trusted code (#331, #424)."""

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = ".claude/skills/dev-workflow"
TEMPLATES = ROOT / SKILL_DIR / "assets" / "workflows"


def _job(text, name, following=None):
    job = text.split(f"\n  {name}:\n", 1)[1]
    return job.split(f"\n  {following}:\n", 1)[0] if following else job


def _run_scripts(job):
    """The body of every ``run: |`` block of a job, each up to the next step."""
    return [part.split("\n      - ", 1)[0] for part in job.split("run: |")[1:]]


def test_pr_check_runs_the_validator_from_the_base_commit():
    text = (ROOT / ".github" / "workflows" / "pr-check.yml").read_text(encoding="utf-8")
    assert re.search(r"^on:\n  pull_request:\n", text, re.MULTILINE)
    assert "pull_request_target" not in text
    job = _job(text, "pr-check", "version-preview")
    checkout = job.split("- uses: actions/checkout", 1)[1].split("- uses: actions/checkout", 1)[0]
    assert "ref: ${{ github.event.pull_request.base.sha }}" in checkout
    assert "persist-credentials: false" in checkout
    assert "contents: read" in job and "issues: read" in job and "pull-requests: read" in job
    assert "write" not in job.split("steps:", 1)[0]
    # Event data (the PR body among it) arrives only through the environment, never interpolated into a script.
    scripts = _run_scripts(job)
    assert scripts and all("${{" not in script for script in scripts)
    assert any('python3 "$script" check-pr' in script for script in scripts)


def test_pr_files_are_read_as_data_and_never_executed():
    job = _job(
        (ROOT / ".github" / "workflows" / "pr-check.yml").read_text(encoding="utf-8"), "pr-check", "version-preview"
    )
    head = job.split("- uses: actions/checkout")[2].split("- name:", 1)[0]
    assert "path: .pr-head" in head
    assert "ref: ${{ github.event.pull_request.head.sha }}" in head and "persist-credentials: false" in head
    runs = "\n".join(part.split("\n      - ", 1)[0] for part in job.split("run: |")[1:])
    assert set(re.findall(r"python3 \S+", runs)) == {'python3 "$script"'}
    assert "check --tree .pr-head" in runs
    assert 'script=".pr-head' not in runs and "cd .pr-head" not in runs


def test_version_preview_writes_only_for_same_repository_prs_with_base_code():
    job = _job((ROOT / ".github" / "workflows" / "pr-check.yml").read_text(encoding="utf-8"), "version-preview")
    condition = next(line.strip() for line in job.splitlines() if line.startswith("    if: "))
    assert "github.event.pull_request.head.repo.full_name == github.repository" in condition
    assert "||" not in condition  # the same-repository condition is never bypassed by an alternative
    scripts = _run_scripts(job)
    assert scripts and all("${{" not in script for script in scripts)
    checkout = job.split("- uses: actions/checkout", 1)[1].split("- name:", 1)[0]
    assert "ref: ${{ github.event.pull_request.base.sha }}" in checkout
    assert "persist-credentials: false" in checkout


def test_installed_workflows_match_the_skill_templates():
    """Every workflow of the skill is installed exactly as its bootstrap generated it (#625).

    The bootstrap renders the templates (placeholders, conditionals, language) and records the
    sha256 of each generated file, with LF line endings, in the manifest; a hand edit changes
    the digest. ``bootstrap.py doctor`` applies the same rule.
    """
    manifest = json.loads((ROOT / ".github" / "dev-workflow-manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == (ROOT / SKILL_DIR / "VERSION").read_text(encoding="utf-8").strip()
    assert manifest["skill_dir"] == SKILL_DIR
    for template in sorted(TEMPLATES.glob("*.yml")):
        name = f".github/workflows/{template.name}"
        text = (ROOT / name).read_text(encoding="utf-8").replace("\r\n", "\n")
        assert manifest["files"].get(name) == hashlib.sha256(text.encode("utf-8")).hexdigest(), name
