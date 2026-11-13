"""Every Gunicorn start command: one worker timeout (#298) and no control socket (#441)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILES = (ROOT / "Dockerfile", ROOT / "deploy" / "agent" / "Dockerfile")
# The web unit is a file since #318; the documentation keeps the metrics example.
DOCUMENTED = (
    ROOT / "docs" / "deployment.md",
    ROOT / "deploy" / "systemd" / "skeleton-web.service",
    ROOT / "compose.sandbox.yaml",  # The sandbox starts Gunicorn itself (#369).
)


def dockerfile_commands(path):
    commands = [
        line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("CMD") and "gunicorn" in line
    ]
    assert len(commands) == 1, (path, commands)
    return commands


def documented_commands():
    commands = [
        line
        for path in DOCUMENTED
        for line in path.read_text(encoding="utf-8").splitlines()
        if re.search(r"\bgunicorn config\.wsgi:application\b", line)
    ]
    assert any(line.startswith("ExecStart=") for line in commands)
    assert any(line.lstrip().startswith("exec gunicorn") for line in commands)
    return commands


def test_gunicorn_timeout_is_the_same_everywhere():
    values = set()
    for path in DOCKERFILES:
        (command,) = dockerfile_commands(path)
        found = re.findall(r'"--timeout", "(\d+)"', command)
        assert len(found) == 1, (path, found)
        values |= set(found)
    for line in documented_commands():
        found = re.findall(r"--timeout (\d+)", line)
        assert len(found) == 1, line
        values |= set(found)
    # The web never waits minutes for the agent any more: previews are prepared asynchronously.
    assert values == {"120"}


def test_gunicorn_never_starts_the_control_socket():
    # Gunicorn >= 25.1 creates $HOME/.gunicorn/gunicorn.ctl by default, which fails on the
    # read-only root (#318) and under ProtectHome; nothing here uses gunicornc.
    for path in DOCKERFILES:
        (command,) = dockerfile_commands(path)
        assert '"--no-control-socket"' in command, path
    for line in documented_commands():
        assert re.search(r"--no-control-socket\b", line), line
