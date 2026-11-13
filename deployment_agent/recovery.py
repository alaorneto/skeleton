"""Crash reconciliation, operator recovery and the offline prune (#245, #317)."""

import shutil
import time
from pathlib import Path

from .execution import sync_state
from .releases import (
    check_drift,
    remove_release,
)
from .storage import (
    AgentError,
    atomic_json,
    deployment_lock,
    identifier,
    load_json,
    log_internal_error,
)


def reconcile_interrupted(service):
    with deployment_lock(service.store.root):
        for operation in service.store.operations():
            if operation["state"] in ("queued", "running") or operation["stage"] in (
                "committed",
                "traffic_release_started",
            ):
                service.stage(operation, "interrupted", state="interrupted", error="agent_interrupted")
                service.maintain(operation)
        for plan_id, record, _ in service.store.plans():
            if record.get("state") != "preparing":
                continue
            # Preparation never touches the active release: no maintenance marker (#298).
            root = Path(service.config["releases_root"])
            for release in root.glob(f"*-{plan_id}") if root.is_dir() else ():
                if release.is_dir() and not release.is_symlink():
                    remove_release(release)
            service.store.put("plans", plan_id, {**record, "state": "failed", "error": "agent_interrupted"})
        state_path = service.store.root / "active.json"
        if state_path.exists():
            state = load_json(state_path)
            try:
                if str(service.backend.active()) != state["release"]:
                    raise AgentError("active_pointer_conflict")
                check_drift(state["release"], state["owned_files"])
            except AgentError:
                atomic_json(service.config["maintenance_marker"], {"stage": "active_state_conflict"}, 0o644)


def recoverable(operation, record, state):
    """The active (release, generation) is still the one before or after this operation."""
    allowed_states = {
        record["previous_release"]: record["plan"]["generation"],
        operation["release"]: record["inventory"]["generation"],
    }
    return state["inventory"]["generation"] == allowed_states.get(state["release"])


def prune(service, *, plan_days=7, backup_days=None, apply=False):
    """List (and with apply, remove) what no operation can still need (#245). Offline CLI only.

    Never removed: the active release, ``release``/``previous_release`` and backup of a failed
    operation that ``recover`` would still accept, the previous release of the last success,
    the project root, the journal and the uploaded artifacts. Backups only with backup_days.
    """
    with deployment_lock(service.store.root):
        service.ensure_available()
        state = service.state()
        operations = service.store.operations()  # Newest first.
        now_time = time.time()
        protected, recoverable_ids = _protected(service, state, operations)
        result = {
            "apply": apply,
            "releases": [],
            "plans": [],
            "images": [],
            "backups": [],
            "skipped": [],
            "errors": [],
        }
        releases = _release_candidates(service, operations, protected, result, plan_days=plan_days, now_time=now_time)
        if backup_days is not None:
            result["backups"] = _backup_candidates(
                service, operations, recoverable_ids, backup_days=backup_days, now_time=now_time
            )
        image = getattr(service.backend, "image", None)
        result["images"] = [image(path) for path in releases] if image else []
        if apply:
            _apply_prune(service, releases, result)
        return result


def _protected(service, state, operations):
    """Releases no prune may remove, and the ids of the failed operations recover still accepts."""
    protected = {Path(state["release"]).resolve()} if state.get("release") else set()
    if service.config.get("project_root"):
        protected.add(Path(service.config["project_root"]).resolve())
    recoverable_ids = set()
    for operation in operations:
        if operation["state"] != "failed":
            continue
        try:
            record = service.store.get("plans", operation["binding"]["plan_id"])
        except AgentError:
            continue
        if recoverable(operation, record, state):
            recoverable_ids.add(operation["id"])
            protected |= {Path(operation["release"]).resolve(), Path(operation["previous_release"]).resolve()}
    last_success = next((row for row in operations if row["state"] == "succeeded"), None)
    if last_success:
        protected |= {Path(last_success["release"]).resolve(), Path(last_success["previous_release"]).resolve()}
    return protected, recoverable_ids


def _release_candidates(service, operations, protected, result, *, plan_days, now_time):
    """Old unsubmitted previews and finished operations; fills ``result`` and returns the paths."""
    root = Path(service.config["releases_root"]).resolve()
    releases = []

    def candidate_release(value, reason):
        path = Path(value).resolve()
        if path.parent != root or Path(value).is_symlink():
            result["skipped"].append({"path": str(value), "reason": "outside_releases_root"})
        elif path in protected or path in releases or not path.is_dir():
            return
        else:
            releases.append(path)
            result["releases"].append({"path": str(path), "reason": reason})

    submitted = {row["binding"]["plan_id"] for row in operations}
    for plan_id, record, mtime in service.store.plans():
        if plan_id in submitted or now_time - mtime < plan_days * 86400:
            continue
        result["plans"].append(plan_id)
        release = (record.get("plan") or {}).get("release")
        if release:
            candidate_release(release, "unsubmitted_preview")
    finished = [row for row in operations[1:] if row["state"] in ("succeeded", "restored", "cancelled")]
    for operation in finished:
        candidate_release(operation["release"], f"operation_{operation['state']}")
    return releases


def _backup_candidates(service, operations, recoverable_ids, *, backup_days, now_time):
    finished_ids = {row["id"] for row in operations if row["state"] in ("succeeded", "restored", "cancelled")}
    backups = []
    for folder in ("backups", "logs"):
        for path in sorted((service.store.root / folder).glob("*")):
            owner = path.name[:36]
            if (
                path.is_dir()
                and not path.is_symlink()
                and owner in finished_ids
                and owner not in recoverable_ids
                and now_time - path.stat().st_mtime >= backup_days * 86400
            ):
                backups.append(str(path))
    return backups


def _apply_prune(service, releases, result):
    for path in releases:
        remove_release(path)
    for plan_id in result["plans"]:
        service.store.path("plans", plan_id).unlink(missing_ok=True)
    for path in result["backups"]:
        shutil.rmtree(path)
    for tag in result["images"]:
        try:
            service.backend.run(
                [service.config.get("docker", "/usr/bin/docker"), "image", "rm", tag], code="image_remove_failed"
            )
        except AgentError as error:
            result["errors"].append({"image": tag, "code": error.code})


def recover(service, operation_id, action, *, acknowledge_new_writes=False):
    with deployment_lock(service.store.root):
        operation = service.store.get("operations", identifier(operation_id))
        if action not in ("restore", "complete", "abort"):
            raise AgentError("invalid_recovery_action")
        if operation["state"] not in ("failed", "interrupted"):
            raise AgentError("operation_not_recoverable", 409)
        record = service.store.get("plans", operation["binding"]["plan_id"])
        state = service.state()
        if not recoverable(operation, record, state):
            raise AgentError("operation_superseded", 409)
        if any(
            row["id"] != operation["id"] and row["state"] in ("queued", "running", "interrupted")
            for row in service.store.operations()
        ):
            raise AgentError("recovery_or_operation_pending", 409)
        with service.backend.logging_to(operation["id"]):
            service.maintain(operation)
            service.backend.drain(service.backend.active(), operation["id"])
            try:
                if action == "complete":
                    check_drift(operation["release"], record["owned_files"])
                    actual = service.backend.probe(operation["release"])
                    approved = {(row["app_label"], row["name"]): row for row in operation["plan"]["migrations"]}
                    if (
                        any(approved.get((row["app_label"], row["name"])) != row for row in actual["migrations"])
                        or actual["targets"] != operation["plan"]["migration_targets"]
                    ):
                        raise AgentError("recovery_migration_plan_changed", 409)
                    if not operation["backup"]:
                        operation["backup"] = service.backend.backup(operation["previous_release"], operation["id"])
                    service.stage(operation, "operator_completion", state="running", error=None)
                    service.finish(operation, record)
                else:
                    writes = operation["traffic_released"]
                    if writes and not acknowledge_new_writes:
                        raise AgentError("new_writes_require_explicit_reconciliation", 409)
                    changed_db = any(
                        event["stage"] in ("migration_started", "migrated", "operator_completion")
                        for event in operation["events"]
                    )
                    if action == "abort" and changed_db:
                        raise AgentError("database_restore_required", 409)
                    previous = Path(operation["previous_release"])
                    if action == "restore":
                        if not operation["backup"]:
                            raise AgentError("backup_not_available", 409)
                        service.stage(operation, "restore_started", state="running")
                        service.backend.restore(operation["backup"], previous)
                    previous_inventory = record["previous_inventory"]
                    previous_owned = record["previous_owned_files"]
                    if action == "restore":
                        service.backend.manage(
                            previous,
                            ["reinstall_record_infrastructure", "--database", "default"],
                            "migration",
                            code="restored_infrastructure_failed",
                        )
                    check_drift(previous, previous_owned)
                    service.backend.manage(
                        previous, ["check_skeleton_schema"], "runtime", code="restored_schema_failed"
                    )
                    service.backend.manage(
                        previous,
                        ["validate_provider_bindings", "--database", "default"],
                        "runtime",
                        code="restored_bindings_failed",
                    )
                    service.backend.manage(previous, ["check", "--deploy"], "runtime", code="restored_checks_failed")
                    service.backend.cutover(previous)
                    service.backend.start(previous, operation["id"])
                    service.health(previous_inventory)
                    service._write_active(previous, previous_inventory, previous_owned)
                    service.stage(
                        operation,
                        "restored" if action == "restore" else "aborted",
                        state="restored" if action == "restore" else "cancelled",
                        error=None,
                        generation=previous_inventory["generation"],
                    )
                    sync_state(service, previous, operation)
                    Path(service.config["maintenance_marker"]).unlink(missing_ok=True)
                return service.public_operation(operation)
            except Exception as error:
                if not isinstance(error, AgentError):
                    log_internal_error(error)
                service.stage(
                    operation,
                    "recovery_failed",
                    state="failed",
                    error=error.code if isinstance(error, AgentError) else type(error).__name__,
                    log=getattr(error, "log", None),
                )
                raise
