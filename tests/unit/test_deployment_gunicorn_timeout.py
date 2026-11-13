"""One explicit Gunicorn worker timeout in both images and in the documented unit (#298)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def dockerfile_timeouts(path):
    commands = [
        line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("CMD") and "gunicorn" in line
    ]
    assert commands, path
    return {match for line in commands for match in re.findall(r'"--timeout", "(\d+)"', line)}, len(commands)


def test_gunicorn_timeout_is_the_same_everywhere():
    values = set()
    for path in (ROOT / "Dockerfile", ROOT / "deploy" / "agent" / "Dockerfile"):
        found, commands = dockerfile_timeouts(path)
        assert len(found) == 1 and commands == 1, (path, found)
        values |= found
    # The web unit is a file since #318; the documentation keeps the metrics example.
    documented = [
        line
        for path in (
            ROOT / "docs" / "deployment.md",
            ROOT / "deploy" / "systemd" / "skeleton-web.service",
            ROOT / "compose.sandbox.yaml",  # The sandbox starts Gunicorn itself (#369).
        )
        for line in path.read_text(encoding="utf-8").splitlines()
        if re.search(r"\bgunicorn config\.wsgi:application\b", line)
    ]
    assert any(line.startswith("ExecStart=") for line in documented)
    for line in documented:
        found = re.findall(r"--timeout (\d+)", line)
        assert len(found) == 1, line
        values |= set(found)
    # The web never waits minutes for the agent any more: previews are prepared asynchronously.
    assert values == {"120"}
