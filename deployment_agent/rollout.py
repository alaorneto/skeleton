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
ACKS = {
    "prepare": "prepared",
    "drain": "drained",
    "cutover": "healthy",
    "resume": "resumed",
    "abort": "aborted",
    "rolling": "switched",
    "restart": "restarted",
}
DRAIN_GRACE_S = 20.0


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
    def begin(
        cls, service: DeploymentService, operation: JSON, record: JSON, *, mode: str = "window"
    ) -> Rollout | None:
        coordinator = getattr(service, "coordinator", None)
        if coordinator is None:
            return None
        coordinator.assert_primary()
        alive = [node for node in coordinator.nodes() if node["alive"]]
        versions = sorted({node["agent_version"] for node in alive if node["agent_version"]})
        if len(versions) > 1:
            # One protocol per operation (#689): update the agents first.
            raise AgentError("agent_versions_differ", 409, [{"code": "agent_version", "found": ", ".join(versions)}])
        nodes = sorted(node["node_id"] for node in alive if node["node_id"] != coordinator.settings.node_id)
        plan = record["plan"]
        coordinator._lock_conn.execute(
            "INSERT INTO skeleton_deploy.rollout (operation_id, epoch, mode, phase, release, release_digest,"
            " base_generation, target_generation, nodes) VALUES (%s, %s, %s, 'prepare', %s, %s, %s, %s, %s)"
            " ON CONFLICT (operation_id) DO UPDATE SET epoch = excluded.epoch, nodes = excluded.nodes",
            (
                operation["id"],
                coordinator.epoch,
                mode,
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
        # A node the operator removed with ``cluster forget`` is no longer waited for (#681).
        present = {node["node_id"] for node in coordinator.nodes()}
        return cls(service, operation_id, [node for node in nodes if node in present])

    def phase(self, phase: str) -> None:
        self.coordinator.assert_primary()
        # The answers of the previous round go first: a phase announced again (``recover complete``)
        # never reads the failure of its first round (#681).
        self.coordinator._lock_conn.execute(
            "UPDATE skeleton_deploy.node SET phase = '' WHERE operation_id = %s", (self.operation_id,)
        )
        self.coordinator._lock_conn.execute(
            "UPDATE skeleton_deploy.rollout SET phase = %s, epoch = %s, phase_started_at = now()"
            " WHERE operation_id = %s",
            (phase, self.coordinator.epoch, self.operation_id),
        )

    def wait_node(self, phase: str, node: str, *, timeout: float, code: str, sleep: Any = time.sleep) -> None:
        """One node's turn of a rolling phase: it alone acts while ``current_node`` names it."""
        self.coordinator.assert_primary()
        self.coordinator._lock_conn.execute(
            "UPDATE skeleton_deploy.node SET phase = '' WHERE operation_id = %s AND node_id = %s",
            (self.operation_id, node),
        )
        self.coordinator._lock_conn.execute(
            "UPDATE skeleton_deploy.rollout SET phase = %s, current_node = %s, epoch = %s, phase_started_at = now()"
            " WHERE operation_id = %s",
            (phase, node, self.coordinator.epoch, self.operation_id),
        )
        expected, deadline = ACKS[phase], time.monotonic() + timeout
        while True:
            answer = self.answers().get(node, "")
            if answer == expected:
                return
            if answer.startswith("failed:"):
                raise AgentError(code, 409, [{"code": answer.removeprefix("failed:"), "component": node}])
            if time.monotonic() >= deadline:
                raise AgentError(code, 409, [{"code": "node_timeout", "component": node}])
            sleep(POLL_S)

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


def begin(service: DeploymentService, operation: JSON, record: JSON, *, mode: str = "window") -> Rollout | None:
    rollout = Rollout.begin(service, operation, record, mode=mode)
    service.rollout = rollout
    return rollout


def current(service: DeploymentService, operation: JSON) -> Rollout | None:
    rollout = getattr(service, "rollout", None)
    if rollout is not None and rollout.operation_id == operation["id"]:
        return rollout
    rollout = Rollout.load(service, operation["id"])
    service.rollout = rollout
    return rollout


# --------------------------------------------------------------------------- node by node


def roll_local(service: Any, previous: Path, release: Path, operation_id: str, inventory: JSON | None = None) -> None:
    """Take this node out of the balancer, stop, switch (when the release changes), start, check (#682).

    On any failure the node goes back to ``previous`` and the error propagates.
    """
    from .execution import health

    marker_path = service.config.get("drain_marker")
    marker = Path(marker_path) if marker_path else None
    backend = service.backend
    try:
        if marker is not None:
            atomic_json(marker, {"operation": operation_id}, 0o644)
            time.sleep(float(service.config.get("drain_grace", DRAIN_GRACE_S)))
        backend.drain(previous, operation_id)
        if release != previous:
            backend.cutover(release)
        backend.start(release, operation_id)
        if marker is not None:
            marker.unlink(missing_ok=True)  # Back in the balancer: the readiness of the check is the real one.
        health(service, inventory or load_json(release / "inventory.json"))
    except Exception:
        if release != previous:
            backend.cutover(previous)
        backend.start(previous, operation_id)
        raise
    finally:
        if marker is not None:
            marker.unlink(missing_ok=True)


def restart_cluster(service: Any, *, operation_id: str | None = None) -> JSON:
    """``cluster restart`` (#682): the same release, node by node, to apply a new configuration."""
    coordinator = _coordinator(service)
    coordinator.assert_primary()
    service.ensure_available()
    state = service.state()
    release = Path(state["release"])
    operation_id = operation_id or f"restart-{uuid4().hex}"
    record = {
        "plan": {
            "release": str(release),
            "release_digest": digest(state["owned_files"]),
            "generation": state["inventory"]["generation"],
        },
        "inventory": state["inventory"],
    }
    run = Rollout.begin(service, {"id": operation_id}, record, mode="rolling")
    if run is None:
        raise AgentError("cluster_not_configured")
    roll_local(service, release, release, operation_id, state["inventory"])
    for node in run.nodes:
        run.wait_node("restart", node, timeout=CUTOVER_TIMEOUT_S, code="rolling_node_failed")
    run.phase("done")
    return {"state": "restarted", "operation_id": operation_id, "nodes": [coordinator.settings.node_id, *run.nodes]}


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


def _force_remove(function: Any, path: str, error: BaseException) -> None:
    """Remove a refused copy even where its frozen modes forbid it (read-only files on some systems)."""
    try:
        os.chmod(os.path.dirname(path), 0o700)  # A frozen parent forbids removing its entries.
        os.chmod(path, 0o700)
        function(path)
    except OSError:
        pass


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
        # The primary's release is frozen (0555 directories): moving a directory to another parent
        # needs write permission on it, and so does removing a refused one. freeze() closes it again.
        for directory in (
            extracted,
            *(path for path in extracted.rglob("*") if path.is_dir() and not path.is_symlink()),
        ):
            os.chmod(directory, 0o755)
        owned = load_json(extracted / "owned-files.json")
        if digest(owned) != release_digest or owned_snapshot(extracted) != owned:
            raise AgentError("follower_release_mismatch", 409)
        os.replace(extracted, target)
        freeze(target)
        return target
    finally:
        archive.unlink(missing_ok=True)
        shutil.rmtree(incoming, onexc=_force_remove)


# --------------------------------------------------------------------------- the follower


class Follower:
    """A follower's (or a non-primary candidate's) side: answer the primary's phases."""

    def __init__(self, service: DeploymentService) -> None:
        self.service = service
        self.coordinator = _coordinator(service)
        # (operation, phase, announcement): a phase announced again (``recover complete``) is a new order.
        self.done: set[tuple[str, str, str]] = set()
        self._round = ""
        self.started_services = False
        self.agent_attempted: str | None = None  # The agent target already tried, once per target (#689).

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
        self.done.add((operation_id, phase, self._round))

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
        operation_id, epoch, phase, name, release_digest, current_node = row[:6]
        if phase in ("rolling", "restart"):
            # One node at a time (#682): act only on this node's turn, once per operation.
            if current_node != self.coordinator.settings.node_id or (operation_id, phase, self._round) in self.done:
                return
            active, _ = self._active()
            if active is None:
                raise AgentError("active_release_missing", 409)
            target = Path(self.service.config["releases_root"]) / name if phase == "rolling" else active
            roll_local(self.service, active, target, operation_id)
            if target != active:
                self.service._write_active(
                    target, load_json(target / "inventory.json"), load_json(target / "owned-files.json")
                )
            self._answer(operation_id, phase, int(epoch), ACKS[phase])
            return
        if (operation_id, phase, self._round) in self.done or phase == "done":
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
        self.follow_agent()

    def follow_agent(self) -> None:
        """Outside an operation, follow the agent version the primary set (#689), once per target."""
        from . import selfupdate

        target = self.coordinator.agent_target()
        if target is None or target["release"] == self.agent_attempted:
            return
        self.agent_attempted = target["release"]
        try:
            selfupdate.follow_agent_target(self.service)
        except AgentError as error:
            self.coordinator.problem = error.code

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
            "SELECT operation_id, epoch, phase, release, release_digest, current_node, phase_started_at"
            " FROM skeleton_deploy.rollout"
            " WHERE epoch = %s ORDER BY started_at DESC LIMIT 1",
            (primary["epoch"],),
        ).fetchone()
        self._round = str(row[6]) if row is not None else ""
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
