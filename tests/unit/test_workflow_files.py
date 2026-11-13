"""Workflows that enforce the issue-first flow run trusted code (#331)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_issue_link_runs_the_validator_from_the_base_commit():
    text = (ROOT / ".github" / "workflows" / "pr-check.yml").read_text(encoding="utf-8")
    assert re.search(r"^on:\n  pull_request:\n", text, re.MULTILINE)
    assert "pull_request_target" not in text
    job = text.split("\n  pr-check:\n", 1)[1].split("\n  version-preview:\n", 1)[0]
    checkout = job.split("- uses: actions/checkout", 1)[1].split("- name:", 1)[0]
    assert "ref: ${{ github.event.pull_request.base.sha }}" in checkout
    assert "persist-credentials: false" in checkout
    assert "contents: read" in job and "issues: read" in job and "pull-requests: read" in job
    assert "write" not in job.split("steps:", 1)[0]
    # The PR body still arrives only through the environment, never interpolated into the script.
    run = job.split("run: |", 1)[1]
    assert "${{" not in run and 'python3 "$script" check-pr' in run


def test_installed_workflow_matches_the_skill_template():
    template = ROOT / ".claude" / "skills" / "dev-workflow" / "assets" / "workflows" / "pr-check.yml"
    rendered = template.read_text(encoding="utf-8").replace("{{SKILL_DIR}}", ".claude/skills/dev-workflow")
    assert (ROOT / ".github" / "workflows" / "pr-check.yml").read_text(encoding="utf-8") == rendered
