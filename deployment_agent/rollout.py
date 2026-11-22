"""The phases of an update across the nodes of a cluster (#681).

The primary runs the operation as on one node (``execution.execute`` and ``finish``) and, at four
points, announces a phase in ``skeleton_deploy.rollout`` and waits for every node that was alive
when the operation started to answer it through its own row of ``skeleton_deploy.node``:

``prepare``   each follower installs the release the primary prepared (``prepared``), before any
              maintenance: a node that cannot fails the operation with no marker and no migration.
``drain``     each node writes its own maintenance marker and stops its services (``drained``),
              before the backup and the migrations; a silent node aborts with no migration.
``cutover``   after the primary's own switch and health check: each follower switches its pointer,
              restarts and checks its own health (``healthy``); a silent node leaves the operation
              ``failed`` under maintenance on every node, for the operator's ``recover``.
``resume``    after the commit: each node removes its marker (``resumed``); then ``done``.
``abort``     the old release again, without the marker, on every node (``aborted``).

The follower never builds a release itself: building one needs the owner profile (the migration
graph probe and the infrastructure export) and its static output is not byte-reproducible across
hosts. It receives the release the primary built and froze, over the authenticated channel (#680),
and installs it only if its ``owned_snapshot`` matches, file by file, the ``owned-files.json`` the
primary recorded in the plan (``release_digest``). The primary holds the owner credential and so is
already trusted with the data; the channel is mutual TLS with an HMAC per request.

A node that was down during an update, or a new node, finds at its next tick that the cluster runs
another release (``skeleton_deploy."primary".release``) and installs it before starting its services
(the launcher waits for the agent with ``--wait-for-agent``), so it never serves old code against a
migrated database.
"""

from __future__ import annotations

import json
import os
import shutil
import tarfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from .storage import AgentError, atomic_json, digest, load_json, log_internal_error
from .types import JSON

if TYPE_CHECKING:
    from .coordination import Coordinator
    from .service import DeploymentService

PREPARE_TIMEOUT_S = 1800.0
DRAIN_TIMEOUT_S = 600.0
CUTOVER_TIMEOUT_S = 900.0
RESUME_TIMEOUT_S = 120.0
POLL_S = 2.0
ACKS = {"prepare": "prepared", "drain": "drained", "cutover": "healthy", "resume": "resumed", "abort": "aborted"}


# --------------------------------------------------------------------------- the primary


def _coordinator(service: DeploymentService) -> Coordinator:
    coordinator = getattr(service, "coordinator", None)
    if coordinator is None:
        raise AgentError("cluster_not_configured")
    return coordinator


class Rollout:
    """The primary's side of one operation across the cluster."""

    def __init__(self, service: DeploymentService, operation_id: str, nodes: list[str]) -> None:
        self.service = service
        self.coordinator = _coordinator(service)
        self.operation_id = operation_id
        self.nodes = nodes

    @classmethod
    def begin(cls, service: DeploymentService, operation: JSON, record: JSON) -> Rollout | None:
        coordinator = getattr(service, "coordinator", None)
        if coordinator is None:
            return None
        coordinator.assert_primary()
        nodes = sorted(
            node["node_id"]
            for node in coordinator.nodes()
            if node["alive"] and node["node_id"] != coordinator.settings.node_id
        )
        plan = record["plan"]
        coordinator._lock_conn.execute(
            "INSERT INTO skeleton_deploy.rollout (operation_id, epoch, mode, phase, release, release_digest,"
            " base_generation, target_generation, nodes) VALUES (%s, %s, 'window', 'prepare', %s, %s, %s, %s, %s)"
            " ON CONFLICT (operation_id) DO UPDATE SET epoch = excluded.epoch, nodes = excluded.nodes",
            (
                operation["id"],
                coordinator.epoch,
                Path(plan["release"]).name,
                plan["release_digest"],
                plan["generation"],
                record["inventory"]["generation"],
                json.dumps(nodes),
            ),
        )
        return cls(service, operation["id"], nodes)

    @classmethod
    def load(cls, service: DeploymentService, operation_id: str) -> Rollout | None:
        coordinator = getattr(service, "coordinator", None)
        if coordinator is None:
            return None
        row = coordinator._execute(
            "SELECT nodes FROM skeleton_deploy.rollout WHERE operation_id = %s", (operation_id,)
        ).fetchone()
        if row is None:
            return None
        nodes = row[0] if isinstance(row[0], list) else json.loads(row[0])
        return cls(service, operation_id, list(nodes))

    def phase(self, phase: str) -> None:
        self.coordinator.assert_primary()
        self.coordinator._lock_conn.execute(
            "UPDATE skeleton_deploy.rollout SET phase = %s, epoch = %s, phase_started_at = now()"
            " WHERE operation_id = %s",
            (phase, self.coordinator.epoch, self.operation_id),
        )

    def answers(self) -> dict[str, str]:
        rows = self.coordinator._execute(
            "SELECT node_id, phase FROM skeleton_deploy.node WHERE operation_id = %s AND phase_epoch = %s",
            (self.operation_id, self.coordinator.epoch),
        ).fetchall()
        return {node_id: phase for node_id, phase in rows}

    def wait(self, phase: str, *, timeout: float, code: str, sleep: Any = time.sleep) -> None:
        """Announce ``phase`` and wait for every node's answer; ``AgentError(code)`` naming the nodes that failed."""
        self.phase(phase)
        expected = ACKS[phase]
        deadline = time.monotonic() + timeout
        while True:
            answers = self.answers()
            failed = {node: answers[node] for node in self.nodes if answers.get(node, "").startswith("failed:")}
            if failed:
                raise AgentError(
                    code,
                    409,
                    [
                        {"code": value.removeprefix("failed:"), "component": node}
                        for node, value in sorted(failed.items())
                    ],
                )
            missing = [node for node in self.nodes if answers.get(node) != expected]
            if not missing:
                return
            if time.monotonic() >= deadline:
                raise AgentError(code, 409, [{"code": "node_timeout", "component": node} for node in missing])
            sleep(POLL_S)


def begin(service: DeploymentService, operation: JSON, record: JSON) -> Rollout | None:
    rollout = Rollout.begin(service, operation, record)
    service.rollout = rollout
    return rollout


def current(service: DeploymentService, operation: JSON) -> Rollout | None:
    rollout = getattr(service, "rollout", None)
    if rollout is not None and rollout.operation_id == operation["id"]:
        return rollout
    rollout = Rollout.load(service, operation["id"])
    service.rollout = rollout
    return rollout


# --------------------------------------------------------------------------- release transfer


def release_archive(service: Any, name: str) -> Path:
    """A tar of a frozen release of this node, for a follower (#681); only known release names."""
    root = Path(service.config["releases_root"])
    release = root / name
    if "/" in name or name.startswith(".") or not release.is_dir() or release.is_symlink():
        raise AgentError("not_found", 404)
    allowed = set()
    try:
        allowed.add(Path(service.state()["release"]).name)
    except AgentError:
        pass
    for _, record, _ in service.store.plans():
        plan = record.get("plan") or {}
        if plan.get("release"):
            allowed.add(Path(plan["release"]).name)
    if name not in allowed:
        raise AgentError("not_found", 404)
    probe = service.store.root / "probe"
    probe.mkdir(exist_ok=True, mode=0o700)
    archive = probe / f"release-{uuid4().hex}.tar"
    with tarfile.open(archive, "w") as tar:
        tar.add(release, arcname=name, recursive=True)
    return archive


def _safe_members(tar: tarfile.TarFile, name: str) -> list[tarfile.TarInfo]:
    members = []
    for member in tar.getmembers():
        parts = Path(member.name).parts
        if not parts or parts[0] != name or ".." in parts or member.name.startswith("/"):
            raise AgentError("unsafe_release_archive", 409)
        if not (member.isdir() or member.isreg() or member.issym()):
            raise AgentError("unsafe_release_archive", 409)
        members.append(member)
    return members


def install_release(service: DeploymentService, name: str, release_digest: str) -> Path:
    """Fetch the primary's frozen release and keep it only if it is, file by file, the planned one."""
    from .releases import freeze, owned_snapshot

    root = Path(service.config["releases_root"])
    target = root / name
    if target.is_dir():
        owned = load_json(target / "owned-files.json")
        if digest(owned) == release_digest and owned_snapshot(target) == owned:
            return target
        raise AgentError("follower_release_mismatch", 409)
    channel = getattr(service, "channel", None)
    if channel is None:
        raise AgentError("cluster_channel_unavailable", 503)
    archive = channel.fetch_release(name)
    incoming = root / f".incoming-{uuid4().hex}"
    incoming.mkdir(mode=0o755)
    try:
        with tarfile.open(archive, "r") as tar:
            tar.extractall(incoming, members=_safe_members(tar, name), filter="tar")
        extracted = incoming / name
        owned = load_json(extracted / "owned-files.json")
        if digest(owned) != release_digest or owned_snapshot(extracted) != owned:
            raise AgentError("follower_release_mismatch", 409)
        os.replace(extracted, target)
        freeze(target)
        return target
    finally:
        archive.unlink(missing_ok=True)
        shutil.rmtree(incoming, ignore_errors=True)


# --------------------------------------------------------------------------- the follower


class Follower:
    """A follower's (or a non-primary candidate's) side: answer the primary's phases."""

    def __init__(self, service: DeploymentService) -> None:
        self.service = service
        self.coordinator = _coordinator(service)
        self.done: set[tuple[str, str]] = set()
        self.started_services = False

    def _active(self) -> tuple[Path | None, JSON]:
        try:
            state = self.service.state()
        except AgentError:
            return None, {}
        return Path(state["release"]), dict(state)

    def _answer(self, operation_id: str, phase: str, epoch: int, answer: str) -> None:
        release, state = self._active()
        generation = (state.get("inventory") or {}).get("generation") if state else None
        self.coordinator.acknowledge(operation_id, answer, epoch, release=str(release or ""), generation=generation)
        self.done.add((operation_id, phase))

    def _switch(self, release: Path, operation: str) -> None:
        from .execution import health

        backend = self.service.backend
        inventory = load_json(release / "inventory.json")
        owned = load_json(release / "owned-files.json")
        backend.cutover(release)
        backend.start(release, operation)
        health(self.service, inventory)
        self.service._write_active(release, inventory, owned)

    def _run(self, row: tuple[Any, ...]) -> None:
        operation_id, epoch, phase, name, release_digest = row
        if (operation_id, phase) in self.done or phase == "done":
            return
        marker = Path(self.service.config["maintenance_marker"])
        active, _ = self._active()
        if phase == "prepare":
            install_release(self.service, name, release_digest)
            answer = "prepared"
        elif phase == "drain":
            atomic_json(marker, {"stage": "cluster_drain", "operation": operation_id}, 0o644)
            if active is not None:
                self.service.backend.drain(active, operation_id)
            answer = "drained"
        elif phase == "cutover":
            target = Path(self.service.config["releases_root"]) / name
            if active is None or active.name != name:
                self._switch(target, operation_id)
            answer = "healthy"
        elif phase == "resume":
            marker.unlink(missing_ok=True)
            answer = "resumed"
        elif phase == "abort":
            marker.unlink(missing_ok=True)
            if active is not None:
                self.service.backend.start(active, operation_id)
            answer = "aborted"
        else:
            return
        self._answer(operation_id, phase, int(epoch), answer)

    def _join(self) -> None:
        """No update in progress: install the release the cluster runs, then start the services once."""
        cluster = self.coordinator.cluster_release()
        active, _ = self._active()
        if cluster is not None and (active is None or active.name != cluster["release"]):
            release = install_release(self.service, cluster["release"], cluster["release_digest"])
            self._switch(release, "cluster-join")
            self.started_services = True
            return
        self.ensure_started()  # The launcher waits for the agent (--wait-for-agent).

    def ensure_started(self) -> None:
        """Once per agent start, outside maintenance: start this node's services (--wait-for-agent)."""
        if self.started_services or Path(self.service.config["maintenance_marker"]).exists():
            return
        active, _ = self._active()
        if active is not None:
            self.service.backend.start(active, "cluster-start")
            self.started_services = True

    def tick(self) -> None:
        primary = self.coordinator.primary()
        if primary is None or self.coordinator.is_primary:
            return
        row = self.coordinator._execute(
            "SELECT operation_id, epoch, phase, release, release_digest FROM skeleton_deploy.rollout"
            " WHERE epoch = %s ORDER BY started_at DESC LIMIT 1",
            (primary["epoch"],),
        ).fetchone()
        try:
            if row is not None and row[2] not in ("done",):
                self._run(row)
            else:
                self._join()
        except AgentError as error:
            if row is not None and row[2] != "done":
                self._answer(row[0], row[2], int(row[1]), f"failed:{error.code}")
            else:
                raise
        except Exception as error:  # noqa: BLE001 - journaled as the node's answer; the primary decides
            log_internal_error(error)
            if row is not None and row[2] != "done":
                self._answer(row[0], row[2], int(row[1]), f"failed:{type(error).__name__}")
