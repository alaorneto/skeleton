"""The entrypoint of the agent container: run the newest usable agent, the volume's or the image's (#772).

``python -m deployment_agent.entry`` is the ``CMD`` of the agent image. A self-update (#497) builds the
new agent under ``agent_root`` (the ``agent_code`` volume, ``/agent/code``), points ``current`` at it and
exits with 75; the container's restart policy starts this entrypoint again, which then runs that version::

    /agent/code/
      current -> versions/0.7.0-1a2b3c4d5e6f      relative symlink, swapped by the agent
      state.json                                  {"current", "previous", "version", ...}
      boot.json                                   {"candidate", "attempts"}, written here
      versions/<version>-<12 hex>/venv/  READY

The image stays the safe base. The volume's agent runs only when it is complete (``READY``, a
readable ``state.json``, a Python in its virtualenv), built for this image's Python ``major.minor``
and **newer** than the image's agent: a newer image wins over the volume. Each start of the volume's
agent counts in ``boot.json``; the agent sets the count back to 0 once its control socket serves
(``selfupdate.confirm_start``). After ``MAX_ATTEMPTS`` starts of the same version that never got
there, this entrypoint runs the image's agent and logs ``agent.entry_fallback``.
``SKELETON_AGENT_USE_IMAGE=1`` always runs the image's agent; ``SKELETON_AGENT_CODE`` is the volume
(``/agent/code``; ``none``: no volume, as ``bootstrap.container_config`` reads it).

Only the standard library and ``packaging`` (installed with the image's agent); it reads no network
and no secret, and writes only ``boot.json``.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from packaging.version import InvalidVersion, Version

DISTRIBUTION = "skeleton-deployment-agent"
DEFAULT_ROOT = "/agent/code"
BOOT_FILE = "boot.json"
MAX_ATTEMPTS = 3
MODULE = "deployment_agent.bootstrap"


def log(event: str, **values: object) -> None:
    print(json.dumps({"event": event, **values}, sort_keys=True), file=sys.stderr, flush=True)


def code_root(environ: Mapping[str, str]) -> Path | None:
    """The agent's code volume from ``SKELETON_AGENT_CODE``; None when switched off or unusable."""
    value = environ.get("SKELETON_AGENT_CODE") or DEFAULT_ROOT  # Compose passes "" when unset.
    if value.lower() == "none":
        return None
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        return None
    return path


def image_version() -> Version | None:
    try:
        return Version(importlib.metadata.version(DISTRIBUTION))
    except (importlib.metadata.PackageNotFoundError, InvalidVersion):
        return None


def _read_json(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_json(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.new")
    temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


def _venv_python_version(venv: Path) -> tuple[int, int] | None:
    """``(major, minor)`` of the Python the virtualenv was made with, from ``pyvenv.cfg``."""
    try:
        lines = (venv / "pyvenv.cfg").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    for line in lines:
        key, _, value = line.partition("=")
        if key.strip() in ("version", "version_info"):
            parts = value.strip().split(".")
            try:
                return int(parts[0]), int(parts[1])
            except (IndexError, ValueError):
                return None
    return None


def candidate(root: Path, python: tuple[int, int]) -> tuple[str, Path, Version] | None:
    """``(name, python, version)`` of the volume's current agent when complete and usable here."""
    link = root / "current"
    if not link.is_symlink():
        return None
    target = os.readlink(link)
    name = target.removeprefix("versions/")
    if target != f"versions/{name}" or not name or "/" in name or name in (".", ".."):
        return None  # The agent only ever writes "versions/<name>" (selfupdate.switch).
    directory = root / "versions" / name
    if directory.is_symlink() or not directory.is_dir() or not (directory / "READY").is_file():
        return None
    venv_python = directory / "venv" / "bin" / "python"
    if not venv_python.exists() or _venv_python_version(directory / "venv") != python:
        return None
    state = _read_json(root / "state.json")
    number = state.get("version") if state is not None else None
    if state is None or state.get("current") != name or not isinstance(number, str):
        return None
    try:
        version = Version(number)
    except InvalidVersion:
        return None
    return name, venv_python, version


def _count_start(root: Path, name: str) -> bool:
    """Count one more start of ``name``; False once it reached ``MAX_ATTEMPTS`` without serving."""
    boot = _read_json(root / BOOT_FILE) or {}
    attempts = boot.get("attempts") if boot.get("candidate") == name else 0
    attempts = attempts if isinstance(attempts, int) and attempts >= 0 else 0
    if attempts >= MAX_ATTEMPTS:
        return False
    try:
        _write_json(root / BOOT_FILE, {"candidate": name, "attempts": attempts + 1})
    except OSError:
        return False  # Without the count a broken version could restart forever: the image instead.
    return True


def choose(
    environ: Mapping[str, str],
    *,
    image: Version | None = None,
    python: tuple[int, int] | None = None,
    executable: str | None = None,
) -> tuple[str, list[str]]:
    """``(executable, argv)`` of the agent to run; the image's unless the volume's is newer and sound."""
    use_image = (executable or sys.executable, [executable or sys.executable, "-m", MODULE])
    if environ.get("SKELETON_AGENT_USE_IMAGE") == "1":
        return use_image
    root = code_root(environ)
    if root is None:
        return use_image
    image = image if image is not None else image_version()
    found = candidate(root, python or (sys.version_info.major, sys.version_info.minor))
    if found is None or image is None:
        return use_image
    name, venv_python, version = found
    if version <= image:
        return use_image  # A newer image wins over the volume.
    if not _count_start(root, name):
        log("agent.entry_fallback", candidate=name, attempts=MAX_ATTEMPTS)
        return use_image
    return str(venv_python), [str(venv_python), "-m", MODULE]


def main(environ: Mapping[str, str] | None = None, execv: Callable[[str, list[str]], object] = os.execv) -> None:
    environ = os.environ if environ is None else environ
    executable, argv = choose(environ)
    if executable != sys.executable:
        log("agent.entry", agent=argv[0])
        try:
            execv(executable, argv)
            return
        except OSError:
            log("agent.entry_fallback", candidate=argv[0], reason="exec_failed")
    execv(sys.executable, [sys.executable, "-m", MODULE])


if __name__ == "__main__":
    main()
