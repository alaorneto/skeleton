"""The entrypoint of the application containers: run the agent's active release, obey its requests (#651).

``python -m deployment_agent.launcher --service web --control /control --state /control-state \\
    --releases-root /releases --active /releases/current --grace 120 -- {venv}/bin/gunicorn ...``

- Resolves the active pointer (a symbolic link the agent switches at cutover), refusing one that
  leaves the releases root, and runs the command with that release's virtualenv and the
  environment the agent gives a release (``SKELETON_PROJECT_ROOT``, ``SKELETON_DEPLOYMENT_INVENTORY``,
  ``STATIC_ROOT``, ``PYTHONPATH``). ``{release}``, ``{venv}`` and ``{python}`` in the command are
  replaced by the release path, its ``.venv`` and its Python.
- Waits while there is no active release (state ``waiting``) and starts as soon as there is one.
- Applies each request of ``<control>/<service>/request.json`` once (``deployment_agent.control``):
  ``stop`` sends SIGTERM to the child, waits up to ``--grace`` seconds (then SIGKILL) and stays
  stopped, also across a restart of the container; ``restart`` stops the child and starts the
  release the pointer names now. Malformed requests are logged and ignored.
- A child that exits on its own ends the launcher with the child's code, so the restart policy of
  the container applies, as with systemd's ``Restart=``.
- SIGTERM to the launcher (the container stopping) stops the child with the same grace.

The owner credential is never passed on: ``MIGRATION_DATABASE_URL`` is removed from the child's
environment even if the container had it. Standard library only.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # Run as a plain file inside the runtime image.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from deployment_agent.control import SERVICE, read_json, valid_request, write_json
else:
    from .control import SERVICE, read_json, valid_request, write_json

POLL_SECONDS = 0.5


def log(message: str) -> None:
    print(f"launcher: {message}", file=sys.stderr, flush=True)


def resolve_active(active: Path, releases_root: Path) -> tuple[Path | None, str]:
    """``(release, "")`` for a usable active release, else ``(None, reason)``."""
    if not active.is_symlink():
        return None, "waiting"
    try:
        release = active.resolve(strict=True)
        root = releases_root.resolve(strict=True)
    except OSError:
        return None, "waiting"
    if release == root or not release.is_relative_to(root) or not release.is_dir():
        return None, "active_release_outside_root"
    if not (release / ".venv" / "bin" / "python").exists():
        return None, "active_release_incomplete"
    return release, ""


def release_environment(release: Path, base: dict[str, str] | None = None) -> dict[str, str]:
    """The environment of the service: the container's own plus the release paths."""
    environment = dict(os.environ if base is None else base)
    environment.pop("MIGRATION_DATABASE_URL", None)  # Never the owner credential in a service.
    venv = release / ".venv"
    environment.update(
        {
            "SKELETON_PROJECT_ROOT": str(release),
            "SKELETON_DEPLOYMENT_INVENTORY": str(release / "inventory.json"),
            "STATIC_ROOT": str(release / "staticfiles"),
            "SKELETON_STATIC_ROOT": str(release / "staticfiles"),
            "PYTHONPATH": str(release),
            "VIRTUAL_ENV": str(venv),
            "PATH": f"{venv / 'bin'}{os.pathsep}{environment.get('PATH', '/usr/local/bin:/usr/bin:/bin')}",
        }
    )
    return environment


def command_for(template: list[str], release: Path) -> list[str]:
    replacements = {
        "{release}": str(release),
        "{venv}": str(release / ".venv"),
        "{python}": str(release / ".venv/bin/python"),
    }
    result = []
    for argument in template:
        for old, new in replacements.items():
            argument = argument.replace(old, new)
        result.append(argument)
    return result


class Launcher:
    def __init__(self, options: argparse.Namespace):
        self.service = options.service
        self.requests = Path(options.control) / self.service / "request.json"
        self.state_file = Path(options.state) / self.service / "state.json"
        self.active = Path(options.active)
        self.releases_root = Path(options.releases_root)
        self.grace = options.grace
        self.template = options.command
        self.child: subprocess.Popen[bytes] | None = None
        self.release: Path | None = None
        previous = read_json(self.state_file) or {}
        self.applied = previous.get("id") if isinstance(previous.get("id"), str) else ""
        self.wanted = "stopped" if previous.get("state") == "stopped" else "running"
        self.status = ""
        self.reason = ""
        self.ignored = ""
        self.terminating = False

    # ------------------------------------------------------------------ state

    def publish(self, state: str, reason: str = "") -> None:
        if (state, reason) == (self.status, self.reason) and state != "running":
            return
        self.status, self.reason = state, reason
        value: dict[str, Any] = {
            "id": self.applied,
            "state": state,
            "release": str(self.release) if self.release else "",
            "pid": self.child.pid if self.child else None,
        }
        if reason:
            value["reason"] = reason
        write_json(self.state_file, value)
        log(f"{self.service} {state}{' (' + reason + ')' if reason else ''}")

    # ------------------------------------------------------------------ child

    def start(self) -> None:
        release, reason = resolve_active(self.active, self.releases_root)
        if release is None:
            self.release = None
            self.publish("waiting" if reason == "waiting" else "failed", reason)
            return
        self.release = release
        command = command_for(self.template, release)
        self.child = subprocess.Popen(command, cwd=release, env=release_environment(release))
        self.publish("running")

    def stop(self) -> None:
        child, self.child = self.child, None
        if child is None or child.poll() is not None:
            return
        child.send_signal(signal.SIGTERM)
        try:
            child.wait(timeout=self.grace)
        except subprocess.TimeoutExpired:
            log(f"{self.service} did not stop within {self.grace}s; killing it")
            child.kill()
            child.wait()

    # ------------------------------------------------------------------ requests

    def apply(self) -> None:
        value = read_json(self.requests)
        if value is None:
            if self.requests.exists() and self.ignored != "unreadable":
                self.ignored = "unreadable"
                log(f"{self.service}: ignoring an unreadable request file")
            return
        if not valid_request(value):
            marker = repr(sorted(value))[:200]
            if self.ignored != marker:
                self.ignored = marker
                log(f"{self.service}: ignoring a malformed request")
            return
        if value["id"] == self.applied:
            return
        self.applied = value["id"]
        self.stop()
        if value["action"] == "stop":
            self.wanted = "stopped"
            self.release = None
            self.publish("stopped")
        else:
            self.wanted = "running"
            self.status = ""  # A restart always publishes, even with the same release.
            self.start()

    def step(self) -> int | None:
        """One pass of the loop; an exit code when the launcher must end."""
        self.apply()
        if self.child is not None:
            code = self.child.poll()
            if code is not None:
                log(f"{self.service} exited with {code}")
                self.child = None
                self.publish("exited", str(code))
                return code if code else 1
        elif self.wanted == "running":
            self.start()
        elif self.status != "stopped":
            self.publish("stopped")
        return None

    def run(self) -> int:
        def terminate(signum: int, frame: Any) -> None:
            self.terminating = True

        signal.signal(signal.SIGTERM, terminate)
        signal.signal(signal.SIGINT, terminate)
        while not self.terminating:
            code = self.step()
            if code is not None:
                return code
            time.sleep(POLL_SECONDS)
        self.stop()
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="skeleton-launcher")
    parser.add_argument("--service", required=True)
    parser.add_argument("--control", required=True, help="Directory of the agent's requests (read-only here)")
    parser.add_argument("--state", required=True, help="Directory of this service's state (written here)")
    parser.add_argument("--releases-root", required=True)
    parser.add_argument("--active", required=True, help="The active release pointer the agent switches")
    parser.add_argument("--grace", type=float, default=30.0, help="Seconds between SIGTERM and SIGKILL")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    options = build_parser().parse_args(argv)
    command = options.command[1:] if options.command[:1] == ["--"] else options.command
    if not SERVICE.fullmatch(options.service) or not command or options.grace <= 0:
        log("invalid arguments")
        return 2
    options.command = command
    return Launcher(options).run()


if __name__ == "__main__":
    raise SystemExit(main())
