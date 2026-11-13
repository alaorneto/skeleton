"""Approval-bound durable operations. The journal is authoritative across DB restores."""

import json
import os
import threading
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from skeleton.system.packages import PackageError, validate_package

from . import adoption, catalogue, execution, planning, recovery
from .backends import backend_for
from .catalogue import NoRedirect  # noqa: F401 - public name, importable from here
from .config import Config
from .execution import APP_ACTIVATION_UPGRADE_CORE  # noqa: F401 - public name, importable from here
from .releases import (
    check_drift,
)
from .storage import (
    AgentError,
    Store,
    atomic_bytes,
    atomic_json,
    canonical,
    file_hash,
    identifier,
    load_json,
    now,
)
from .types import JSON, ActiveState, Plan, StrPath


class DeploymentService:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.store = Store(config["storage_root"])
        (self.store.root / "probe").mkdir(exist_ok=True, mode=0o700)
        Path(config["releases_root"]).mkdir(parents=True, exist_ok=True, mode=0o755)
        Path(config["maintenance_marker"]).parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        self.backend = backend_for(config, self.store)
        self.worker: threading.Thread | None = None
        self._drift: tuple[str, float, str, str | None] | None = None
        self._drift_lock = Lock()

    def state(self) -> ActiveState:
        path = self.store.root / "active.json"
        if not path.exists():
            raise AgentError("deployment_not_initialized", 409)
        return load_json(path)

    def _write_active(self, release: StrPath, inventory: JSON, owned_files: JSON) -> None:
        """The active release changed: record it and drop the inventory's drift cache (#281)."""
        atomic_json(
            self.store.root / "active.json",
            {"release": str(release), "inventory": inventory, "owned_files": owned_files},
        )
        self._drift = None

    def drift_status(self, state: ActiveState, *, max_age: float) -> tuple[str | None, str]:
        """``(error code or None, checked_at)`` for inventory(); informative only (#281).

        A recent result for the same release is reused, so opening the updates page does not hash
        the whole release each time. preview, execute, finish, recover and reconcile_interrupted
        keep calling check_drift themselves, never this cache.
        """
        if str(self.backend.active()) != state["release"]:
            return "active_pointer_conflict", now()
        if not hasattr(self, "_drift_lock"):  # Services built without __init__ (tests).
            self._drift, self._drift_lock = None, Lock()
        with self._drift_lock:
            cached = self._drift
            if max_age and cached and cached[0] == state["release"] and time.monotonic() - cached[1] < max_age:
                return cached[3], cached[2]
            try:
                check_drift(state["release"], state["owned_files"])
                error = None
            except AgentError as exc:
                error = exc.code
            self._drift = (state["release"], time.monotonic(), now(), error)
            return error, self._drift[2]

    def inventory(self) -> JSON:
        state = self.state()
        result = json.loads(canonical(state["inventory"]))
        rows = self.store.operations(limit=50)
        result["operations"] = [self.public_operation(row, include_plan=False) for row in rows]
        result["maintenance"] = Path(self.config["maintenance_marker"]).exists()
        error, checked_at = self.drift_status(state, max_age=self.config.get("drift_check_interval", 300))
        if error:
            result["error"] = error
        result["drift_checked_at"] = checked_at
        return result

    def upload(self, data: bytes) -> JSON:
        maximum = self.config.get("max_upload_bytes", 128 * 1024 * 1024)
        if not isinstance(data, bytes) or not data or len(data) > maximum:
            raise AgentError("invalid_upload_size", 413)
        temporary = self.store.root / "uploads" / f".{uuid4()}.upload"
        atomic_bytes(temporary, data)
        try:
            try:
                package = validate_package(temporary, self.config["approved_publishers"])
            except PackageError:
                raise AgentError("package_verification_failed") from None
            destination = self.store.path("uploads", package.digest)
            if destination.exists():
                if file_hash(destination) != package.digest:
                    raise AgentError("stored_artifact_corrupted", 409)
            else:
                os.replace(temporary, destination)
                os.chmod(destination, 0o400)
            return {"digest": package.digest, "manifest": package.manifest}
        finally:
            temporary.unlink(missing_ok=True)

    def initialize(
        self, inventory_path: StrPath, *, core_package: StrPath, app_packages: Iterable[StrPath] = ()
    ) -> JSON:
        return adoption.initialize(self, inventory_path, core_package=core_package, app_packages=app_packages)

    def ensure_available(self, *, ignore_plan: str | None = None) -> None:
        if Path(self.config["maintenance_marker"]).exists() or any(
            state in ("queued", "running", "interrupted") for state in self.store.operation_states().values()
        ):
            raise AgentError("recovery_or_operation_pending", 409)
        # One preparation at a time (#298); prepare() ignores its own record.
        if any(
            record.get("state") == "preparing" and plan_id != ignore_plan for plan_id, record, _ in self.store.plans()
        ):
            raise AgentError("recovery_or_operation_pending", 409)

    def plan_status(self, plan_id: str) -> JSON:
        return planning.plan_status(self, plan_id)

    def preview(self, action: str, artifacts: list[str] | None = None, app_ids: list[str] | None = None) -> JSON:
        """Validate quickly, record the plan as ``preparing`` and build it in a thread (#298).

        The web request no longer waits for the release copy, pip, checks and image build:
        it receives ``{id, state}`` and follows ``GET /plans/<id>``.
        """
        plan_id = planning.request_preview(self, action, artifacts, app_ids)
        # The lock is released before the thread starts; prepare() takes it again and rechecks.
        self.worker = threading.Thread(target=self.prepare, args=(plan_id,), daemon=False, name=f"preview-{plan_id}")
        self.worker.start()
        return {"id": plan_id, "state": "preparing"}

    def prepare(self, plan_id: str) -> Plan | None:
        return planning.prepare(self, plan_id)

    def submit(
        self, plan_id: str, plan_digest: str, generation: int, operation_id: str, actor_id: str, request_id: str
    ) -> JSON:
        operation, created = execution.queue_operation(
            self, plan_id, plan_digest, generation, operation_id, actor_id, request_id
        )
        if not created:
            return self.public_operation(operation)
        operation_id = operation["id"]
        self.worker = threading.Thread(
            target=self.execute, args=(operation_id,), daemon=False, name=f"deployment-{operation_id}"
        )
        self.worker.start()
        return self.public_operation(operation)

    def public_operation(self, operation: Mapping[str, Any], include_plan: bool = True) -> JSON:
        keys = (
            "id",
            "operation",
            "action",
            "state",
            "stage",
            "generation",
            "expected_generation",
            "plan_digest",
            "backup",
            "release",
            "error",
            "created_at",
            "updated_at",
            "traffic_released",
        )
        result = {key: operation.get(key) for key in keys}
        if include_plan:
            result["plan"] = operation["plan"]
        return result

    def operation(self, operation_id: str) -> JSON:
        return self.public_operation(self.store.get("operations", identifier(operation_id)))

    def stage(self, operation: JSON, stage: str, *, log: str | None = None, **changes: Any) -> None:
        operation.update(changes)
        operation["stage"] = stage
        operation["updated_at"] = now()
        event = {"stage": stage, "at": operation["updated_at"]}
        if log:
            event["log"] = log  # Only the file name under logs/<operation>/ (#244), never its content.
        operation["events"].append(event)
        self.store.put("operations", operation["id"], operation)

    def maintain(self, operation: JSON) -> None:
        atomic_json(
            self.config["maintenance_marker"], {"operation": operation["id"], "stage": operation["stage"]}, 0o644
        )

    def execute(self, operation_id: str) -> None:
        return execution.execute(self, operation_id)

    def finish(self, operation: JSON, record: JSON) -> None:
        return execution.finish(self, operation, record)

    def health(self, inventory: JSON) -> None:
        return execution.health(self, inventory)

    def reconcile_interrupted(self) -> None:
        return recovery.reconcile_interrupted(self)

    @staticmethod
    def _recoverable(operation: JSON, record: JSON, state: ActiveState) -> bool:
        return recovery.recoverable(operation, record, state)

    def prune(self, *, plan_days: float = 7, backup_days: float | None = None, apply: bool = False) -> JSON:
        return recovery.prune(self, plan_days=plan_days, backup_days=backup_days, apply=apply)

    def recover(self, operation_id: str, action: str, *, acknowledge_new_writes: bool = False) -> JSON:
        return recovery.recover(self, operation_id, action, acknowledge_new_writes=acknowledge_new_writes)

    def catalogue_suggestions(self, dependency: str, required: str, target: JSON, *, limit: int = 3) -> list[JSON]:
        return catalogue.catalogue_suggestions(self, dependency, required, target, limit=limit)

    def catalogue_sources(self) -> list[JSON]:
        return catalogue.catalogue_sources(self)

    def fetch_bytes(
        self, location: str, maximum: int, *, origin: tuple[str, str | None, int | None] | None = None
    ) -> bytes:
        return catalogue.fetch_bytes(self, location, maximum, origin=origin)

    def check_updates(self) -> JSON:
        return catalogue.check_updates(self)

    def fetch_release(self, component_id: str, version: str, source_id: str) -> JSON:
        return catalogue.fetch_release(self, component_id, version, source_id)
