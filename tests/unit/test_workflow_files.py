"""Workflows that enforce the issue-first flow run trusted code (#331, #424)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = ".claude/skills/dev-workflow"
TEMPLATES = ROOT / SKILL_DIR / "assets" / "workflows"


def _job(text, name, following=None):
    job = text.split(f"\n  {name}:\n", 1)[1]
    return job.split(f"\n  {following}:\n", 1)[0] if following else job


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
    # The PR body still arrives only through the environment, never interpolated into the script.
    run = job.split("run: |", 1)[1]
    assert "${{" not in run and 'python3 "$script" check-pr' in run


def test_pr_files_are_read_as_data_and_never_executed():
    job = _job(
        (ROOT / ".github" / "workflows" / "pr-check.yml").read_text(encoding="utf-8"), "pr-check", "version-preview"
    )
    head = job.split("- uses: actions/checkout")[2].split("- name:", 1)[0]
    assert "path: .pr-head" in head
    assert "ref: ${{ github.event.pull_request.head.sha }}" in head and "persist-credentials: false" in head
    runs = "\n".join(part.split("\n      - ", 1)[0] for part in job.split("run: |")[1:])
    assert re.findall(r"python3 \S+", runs) == ['python3 "$script"', 'python3 "$script"']
    assert "check --tree .pr-head" in runs
    assert 'script=".pr-head' not in runs and "cd .pr-head" not in runs


def test_version_preview_writes_only_for_same_repository_prs_with_base_code():
    job = _job((ROOT / ".github" / "workflows" / "pr-check.yml").read_text(encoding="utf-8"), "version-preview")
    assert "if: github.event.pull_request.head.repo.full_name == github.repository" in job
    checkout = job.split("- uses: actions/checkout", 1)[1].split("- name:", 1)[0]
    assert "ref: ${{ github.event.pull_request.base.sha }}" in checkout
    assert "persist-credentials: false" in checkout


def test_installed_workflows_match_the_skill_templates():
    for template in sorted(TEMPLATES.glob("*.yml")):
        installed = ROOT / ".github" / "workflows" / template.name
        rendered = (
            template.read_text(encoding="utf-8")
            .replace("__SKILL_GLOB__", f"{SKILL_DIR}/**")
            .replace("__SKILL_DIR__", SKILL_DIR)
            .replace("__DEFAULT_BRANCH__", "main")
            .replace("__RELEASE_LABEL__", "release")
        )
        assert installed.read_text(encoding="utf-8") == rendered, template.name
