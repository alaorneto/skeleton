"""The agent's version and its self-update from a signed release package (#497).

A virtualenv cannot be moved and a process does not replace its running code, so an update builds
a new virtualenv beside the current one, tests it (``selfcheck``), swaps the ``current`` symlink
atomically and exits with ``RESTART_EXIT_CODE`` for the supervisor to start the new version::

    <agent_root>/
      current -> versions/0.6.0-1a2b3c4d5e6f      relative symlink
      state.json                                  {current, previous, version, release, switched_at}
      versions/<version>-<12 hex of the release digest>/
        venv/  wheels/  requirements.lock  READY

Only a release package signed by an approved publisher that also published the installed core
(or one the operator lists in ``agent_publishers``) replaces the agent. Installation is offline
and hash-locked; command output never leaves the agent.
"""

import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from packaging.markers import default_environment
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from skeleton.system.packages import (
    PackageError,
    extract_package,
    match_release_target,
    read_release_member,
    validate_package,
    validate_release,
)

from .storage import AgentError, artifact_id, atomic_bytes, atomic_json, deployment_lock, file_hash, load_json, now
from .types import JSON, StrPath

if TYPE_CHECKING:
    from skeleton.system.packages import VerifiedRelease

    from .config import Config
    from .service import DeploymentService

RESTART_EXIT_CODE = 75
DISTRIBUTION = "skeleton-deployment-agent"
Runner = Callable[..., Any]


def agent_version() -> str | None:
    """The installed agent distribution's version; None from a source checkout (no lock then)."""
    try:
        return importlib.metadata.version(DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError:
        return None


def agent_root(config: "Config | Mapping[str, Any]", root: StrPath | None = None) -> Path:
    value = root if root is not None else config.get("agent_root")
    if not value:
        raise AgentError("agent_self_update_unavailable", 409)
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise AgentError("agent_self_update_unavailable", 409)
    return path


def check_release(
    config: "Config | Mapping[str, Any]", verified: "VerifiedRelease", *, core_publisher: str | None
) -> JSON:
    """The target of this interpreter in a release package allowed to replace the agent."""
    agent = verified.manifest.get("agent")
    if not agent:
        raise AgentError("release_without_agent", 409)
    allowed = set(config.get("agent_publishers") or ())
    if core_publisher:
        allowed.add(core_publisher)
    if verified.manifest["publisher"] not in allowed:
        raise AgentError("agent_publisher_not_allowed", 403)
    current = agent_version()
    if current is not None:
        try:
            new, old = Version(agent["version"]), Version(current)
        except InvalidVersion:
            raise AgentError("package_verification_failed", 409) from None
        if new == old:
            raise AgentError("agent_already_current", 409)
        if new < old:
            raise AgentError("agent_downgrade_forbidden", 409)
    # The agent's own interpreter, not the backend's: the agent runs on the host even with Compose.
    target = match_release_target(verified.manifest, default_environment())
    if target is None:
        raise AgentError("agent_target_unavailable", 409)
    return target


def _environment(config: "Config | Mapping[str, Any]") -> dict[str, str]:
    return {
        "PATH": config.get("operator_path", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "HOME": str(config.get("storage_root", "/nonexistent")),
        "PYTHONNOUSERSITE": "1",
    }


def _run(
    run: Runner, config: "Config | Mapping[str, Any]", arguments: Sequence[StrPath], *, capture: bool = False
) -> bytes:
    """One fixed command; its output is discarded (or returned as data), never logged."""
    result = run(
        [str(argument) for argument in arguments],
        env=_environment(config),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
        timeout=config.get("command_timeout", 1800),
    )
    output = getattr(result, "stdout", b"") or b""
    return output if isinstance(output, bytes) else b""


def _core_wheel(config: "Config | Mapping[str, Any]", verified: "VerifiedRelease", wheels: Path) -> Path:
    """Verify the embedded core package and extract its wheels; the django-skeleton wheel."""
    with tempfile.TemporaryDirectory(prefix="skeleton-agent-core-", dir=wheels.parent) as temporary:
        package_path = Path(temporary) / "core.skapp"
        atomic_bytes(package_path, read_release_member(verified, verified.manifest["core"]["path"]))
        package = validate_package(package_path, config["approved_publishers"])
        extract_package(package, None, wheels_destination=Path(temporary) / "wheels")
        for wheel in (Path(temporary) / "wheels").iterdir():
            shutil.move(str(wheel), wheels / wheel.name)
    names = [
        Path(item["wheel"]).name
        for item in verified.core_manifest["python_dependencies"]
        if canonicalize_name(item["name"]) == "django-skeleton"
    ]
    if len(names) != 1 or not (wheels / names[0]).is_file():
        raise AgentError("agent_build_failed", 409)
    return wheels / names[0]


def version_name(verified: "VerifiedRelease") -> str:
    return f"{verified.manifest['agent']['version']}-{verified.digest[:12]}"


def build_version(
    config: "Config", verified: "VerifiedRelease", target: JSON, *, root: Path, run: Runner = subprocess.run
) -> Path:
    """Build and test ``versions/<name>``; a partial directory is always removed."""
    agent = verified.manifest["agent"]
    directory = root / "versions" / version_name(verified)
    if (directory / "READY").is_file():
        return directory
    if directory.exists():
        shutil.rmtree(directory)
    try:
        wheels = directory / "wheels"
        wheels.mkdir(parents=True, mode=0o755)
        for member in [*target["wheels"], agent["wheel"]]:
            atomic_bytes(wheels / Path(member).name, read_release_member(verified, member), 0o644)
        core = _core_wheel(config, verified, wheels)
        core_version = next(
            item["version"]
            for item in verified.core_manifest["python_dependencies"]
            if canonicalize_name(item["name"]) == "django-skeleton"
        )
        lock = read_release_member(verified, target["lock"]).decode("utf-8").rstrip("\n")
        lock += f"\ndjango-skeleton=={core_version} --hash=sha256:{file_hash(core)}"
        lock += f"\n{DISTRIBUTION}=={agent['version']} --hash=sha256:{agent['sha256']}\n"
        atomic_bytes(directory / "requirements.lock", lock.encode("utf-8"), 0o644)
        python = directory / "venv" / "bin" / "python"
        _run(run, config, [sys.executable, "-m", "venv", directory / "venv"])
        _run(
            run,
            config,
            [
                python,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-index",
                "--only-binary=:all:",
                "--find-links",
                wheels,
                "--require-hashes",
                "-r",
                directory / "requirements.lock",
            ],
        )
        _run(run, config, [python, "-m", "pip", "check"])
        launcher = directory / "venv" / "bin" / "skeleton-deployment-agent"
        configuration = getattr(config, "path", None) or config.get("config_path")
        if not configuration:
            raise AgentError("agent_build_failed", 409)
        report = json.loads(_run(run, config, [launcher, "--config", configuration, "selfcheck"], capture=True))
        if not isinstance(report, dict) or report.get("ok") is not True or report.get("version") != agent["version"]:
            raise AgentError("agent_build_failed", 409)
        atomic_bytes(directory / "READY", b"", 0o644)
        return directory
    except Exception as error:
        shutil.rmtree(directory, ignore_errors=True)
        if isinstance(error, AgentError) and error.code != "agent_build_failed":
            raise
        raise AgentError("agent_build_failed", 409) from None  # Never the pip/venv output.


def _state(root: Path) -> JSON:
    path = root / "state.json"
    return load_json(path) if path.exists() else {}


def switch(root: Path, directory: Path, *, release: str, version: str) -> JSON:
    """Point ``current`` at ``directory`` atomically and keep only current and previous versions."""
    current = root / "current"
    old = Path(os.readlink(current)).name if current.is_symlink() else None
    previous = old if old and old != directory.name else _state(root).get("previous")
    staged = root / "current.new"
    staged.unlink(missing_ok=True)
    os.symlink(f"versions/{directory.name}", staged)
    os.replace(staged, current)  # current never stops existing.
    state = {
        "current": directory.name,
        "previous": previous,
        "version": version,
        "release": release,
        "switched_at": now(),
    }
    atomic_json(root / "state.json", state, 0o644)
    for child in (root / "versions").iterdir():
        if child.name not in (directory.name, previous) and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
    return state


def _verified(config: "Config | Mapping[str, Any]", path: Path) -> "VerifiedRelease":
    try:
        return validate_release(path, config["approved_publishers"])
    except PackageError:
        raise AgentError("package_verification_failed", 409) from None


def _core_publisher(service: "DeploymentService") -> str | None:
    try:
        publisher = (service.state()["inventory"].get("core") or {}).get("publisher")
    except AgentError:
        return None
    return publisher if isinstance(publisher, str) else None


def update(
    service: "DeploymentService", release_digest: str, *, actor_id: str | None = None, run: Runner = subprocess.run
) -> JSON:
    """``POST /agent/update``: build, test and switch to the agent of an uploaded release package."""
    root = agent_root(service.config)
    with deployment_lock(service.store.root):
        service.ensure_available()
        verified = _verified(service.config, service.store.path("uploads", artifact_id(release_digest)))
        target = check_release(service.config, verified, core_publisher=_core_publisher(service))
        directory = build_version(service.config, verified, target, root=root, run=run)
        version = verified.manifest["agent"]["version"]
        previous = agent_version()
        switch(root, directory, release=verified.digest, version=version)
        journal = service.store.root / "agent-updates"
        journal.mkdir(exist_ok=True, mode=0o700)
        atomic_json(
            journal / f"{uuid4()}.json",
            {
                "event": "agent_update",
                "from": previous,
                "to": version,
                "release": verified.digest,
                "actor_id": actor_id,
                "at": now(),
            },
        )
    return {"state": "restarting", "version": version}


def install_from_file(
    config: "Config", path: StrPath, *, root: StrPath | None = None, run: Runner = subprocess.run
) -> JSON:
    """``agent-install``: install or update the agent from a release package file (offline CLI)."""
    directory_root = agent_root(config, root)
    with deployment_lock(config["storage_root"], blocking=True):
        verified = _verified(config, Path(path))
        # From a file, the operator chose the package: any approved publisher may provide it.
        target = check_release(config, verified, core_publisher=verified.manifest["publisher"])
        directory = build_version(config, verified, target, root=directory_root, run=run)
        state = switch(
            directory_root, directory, release=verified.digest, version=verified.manifest["agent"]["version"]
        )
    return {
        **state,
        "next_steps": [
            f"ExecStart={directory_root / 'current' / 'venv' / 'bin' / 'skeleton-deployment-agent'} --config <agent.json> serve",
            "systemctl daemon-reload",
            "systemctl restart skeleton-agent",
        ],
    }


def rollback(config: "Config | Mapping[str, Any]", *, root: StrPath | None = None) -> JSON:
    """``agent-rollback``: point ``current`` back at the previous ready version (offline CLI)."""
    directory_root = agent_root(config, root)
    state = _state(directory_root)
    previous = state.get("previous")
    if not previous or not (directory_root / "versions" / str(previous) / "READY").is_file():
        raise AgentError("agent_rollback_unavailable", 409)
    current = directory_root / "current"
    staged = directory_root / "current.new"
    staged.unlink(missing_ok=True)
    os.symlink(f"versions/{previous}", staged)
    os.replace(staged, current)
    restored = {
        "current": previous,
        "previous": state.get("current"),
        "version": str(previous).rsplit("-", 1)[0],
        "release": None,
        "switched_at": now(),
    }
    atomic_json(directory_root / "state.json", restored, 0o644)
    return {**restored, "next_steps": ["systemctl restart skeleton-agent"]}


def selfcheck(config: "Config") -> JSON:
    """``selfcheck``: the new agent loads its configuration and modules; writes nothing."""
    from . import catalogue, environments, paths, planning, releases, server  # noqa: F401 - import check

    if not Path(config["storage_root"]).is_dir():
        raise AgentError("storage_root_missing")
    return {"ok": True, "version": agent_version()}
