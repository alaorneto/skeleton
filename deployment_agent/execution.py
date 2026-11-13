"""Execution of an approved operation, from the write gate to traffic release (#317)."""

import json
import shutil
import time
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import Request, build_opener

from packaging.version import Version

from .catalogue import NoRedirect
from .releases import (
    check_drift,
    verified,
)
from .storage import (
    AgentError,
    artifact_id,
    deployment_lock,
    digest,
    identifier,
    load_json,
    log_internal_error,
    now,
    private_file,
)

# First core release whose apps are closed by default and that ships ``upgrade_app_activation`` (#22).
APP_ACTIVATION_UPGRADE_CORE = Version("0.5.0")


def graph_matches(graph, plan):
    """The probed migration graph is still the approved one."""
    return (
        graph["migrations"] == plan["migrations"]
        and graph["applied"] == plan["applied_migrations"]
        and graph["targets"] == plan["migration_targets"]
    )


def sync_state(service, release, operation):
    """Mirror the journal and the inventory of ``release`` into the database (owner profile)."""
    service.backend.manage(
        release,
        [
            "sync_deployment_state",
            "--inventory",
            Path(release) / "inventory.json",
            "--operation",
            service.store.path("operations", operation["id"]),
        ],
        "migration",
        code="state_sync_failed",
    )


def execute(service, operation_id):
    with deployment_lock(service.store.root, blocking=True):
        operation = service.store.get("operations", operation_id)
        if operation["state"] != "queued":
            return
        with service.backend.logging_to(operation_id):
            try:
                record = service.store.get("plans", operation["binding"]["plan_id"])
                plan = record["plan"]
                state = service.state()
                if (
                    state["inventory"]["generation"] != plan["generation"]
                    or state["release"] != record["previous_release"]
                    or digest(state["owned_files"]) != record["source_owned_digest"]
                ):
                    raise AgentError("stale_execution", 409)
                if str(service.backend.active()) != state["release"]:
                    raise AgentError("active_pointer_conflict", 409)
                if shutil.disk_usage(service.config["releases_root"]).free < service.config.get(
                    "minimum_free_bytes", 512 * 1024 * 1024
                ):
                    raise AgentError("insufficient_disk_space", 409)
                check_drift(state["release"], state["owned_files"])
                check_drift(plan["release"], record["owned_files"])
                for artifact in plan["artifacts"]:
                    verified(service.store, service.config, artifact)
                graph = service.backend.probe(plan["release"])
                if not graph_matches(graph, plan):
                    raise AgentError("migration_plan_changed", 409)
                # Configuration checked before any effect: no marker, no drain on a typo (#246).
                service.backend.preflight(state["release"])
                service.stage(operation, "maintenance_entering", state="running")
                service.maintain(operation)
                service.backend.drain(state["release"], operation_id)
                service.stage(operation, "drained")
                backup = service.backend.backup(state["release"], operation_id)
                service.stage(operation, "backup_ready", backup=backup)
                # Recheck under the write gate; no DB changes may escape approval between preview and drain.
                graph = service.backend.probe(plan["release"])
                if not graph_matches(graph, plan):
                    raise AgentError("migration_plan_changed", 409)
                service.stage(operation, "migration_started")
                service.finish(operation, record)
            except Exception as error:  # noqa: BLE001 - any failure is journaled before re-raising
                if not isinstance(error, AgentError):
                    log_internal_error(error)  # Class and stack only, never the message (#244).
                code = error.code if isinstance(error, AgentError) else type(error).__name__
                service.stage(operation, "failed", state="failed", error=code, log=getattr(error, "log", None))


def finish(service, operation, record):
    release = Path(operation["release"])
    service.backend.manage(release, ["migrate", "--noinput"], "migration", code="migration_failed")
    service.stage(operation, "migrated")
    service.backend.manage(release, ["sync_table_catalog"], "migration", code="catalogue_sync_failed")
    if Version(record["inventory"]["core"]["version"]) >= APP_ACTIVATION_UPGRADE_CORE:
        # Closed-by-default activation (#22): keep what organizations used before; idempotent per run.
        service.backend.manage(release, ["upgrade_app_activation"], "runtime", code="app_activation_upgrade_failed")
    service.backend.manage(release, ["check_skeleton_schema"], "runtime", code="runtime_schema_failed")
    if Version(record["inventory"]["core"]["version"]) >= Version("0.2.0"):
        service.backend.manage(
            release, ["check_application_configuration"], "runtime", code="application_configuration_invalid"
        )
    service.backend.manage(
        release, ["validate_provider_bindings", "--database", "default"], "runtime", code="provider_bindings_failed"
    )
    service.backend.manage(release, ["check", "--deploy"], "runtime", code="deployment_checks_failed")
    service.stage(operation, "runtime_checked")
    check_drift(release, record["owned_files"])
    service.stage(operation, "cutover_started")
    service.backend.cutover(release)
    service.stage(operation, "release_switched")
    service.backend.start(release, operation["id"])
    service.stage(operation, "workers_started")
    service.health(record["inventory"])
    service.stage(operation, "health_verified", generation=record["inventory"]["generation"])
    sync_state(service, release, operation)
    service._write_active(release, record["inventory"], record["owned_files"])
    service.stage(operation, "committed", state="succeeded")
    sync_state(service, release, operation)
    # Journal write intent precedes traffic release; an interrupted release is conservatively post-writes.
    service.stage(operation, "traffic_release_started", traffic_released=True)
    Path(service.config["maintenance_marker"]).unlink(missing_ok=True)
    service.stage(operation, "ready", state="succeeded")


def health(service, inventory):
    opener = build_opener(NoRedirect())
    deadline = time.monotonic() + service.config.get("health_timeout", 60)
    urls = service.config["health_urls"]
    release_url = service.config.get("release_health_url", urljoin(urls[0], "/health/release/"))
    last = "health_failed"
    while time.monotonic() < deadline:
        try:
            headers = {"Host": service.config["health_host"]} if service.config.get("health_host") else {}
            if service.config.get("smoke_token_file"):
                token = private_file(service.config["smoke_token_file"])  # Same rule as the control token.
                headers["X-Skeleton-Deployment-Smoke"] = token.read_text().strip()
            with opener.open(Request(release_url, headers=headers), timeout=5) as response:  # noqa: S310 - health_urls are http(s) only, validated by Config.load
                result = json.loads(response.read(4 * 1024 * 1024))
            if (
                result.get("generation") != inventory["generation"]
                or result.get("core", {}).get("version") != inventory["core"]["version"]
                or result.get("core", {}).get("digest") != inventory["core"]["digest"]
                or result.get("core", {}).get("origin") != inventory["core"]["origin"]
            ):
                raise AgentError("worker_release_mismatch", 409)
            for url in urls:
                with opener.open(Request(url, headers=headers), timeout=5) as response:  # noqa: S310 - health_urls are http(s) only, validated by Config.load
                    if response.status != 200:
                        raise AgentError("application_smoke_failed", 409)
            return
        except Exception as error:  # noqa: BLE001 - any failure is retried until the health deadline
            last = error.code if isinstance(error, AgentError) else "health_unavailable"
            time.sleep(0.5)
    raise AgentError(last, 409)


def queue_operation(service, plan_id, plan_digest, generation, operation_id, actor_id, request_id):
    """Journal the approved binding; ``(operation, created)``, ``created`` False for a repeated delivery."""
    operation_id = identifier(operation_id)
    plan_id = identifier(plan_id)
    actor_id = identifier(actor_id)
    request_id = identifier(request_id)
    artifact_id(plan_digest)
    if type(generation) is not int or generation < 0:
        raise AgentError("invalid_generation")
    binding = {
        "plan_id": plan_id,
        "plan_digest": plan_digest,
        "generation": generation,
        "actor_id": actor_id,
        "request_id": request_id,
    }
    # Atomic journal replacement makes an existing binding safe to read while
    # execution owns the lock. Lost ACKs must not turn into deployment_busy.
    path = service.store.path("operations", operation_id)
    if path.exists():
        operation = load_json(path)
        if operation["binding"] != binding:
            raise AgentError("idempotency_conflict", 409)
        return operation, False
    with deployment_lock(service.store.root):
        # Recheck after locking: another submit may have journaled this UUID.
        if path.exists():
            operation = load_json(path)
            if operation["binding"] != binding:
                raise AgentError("idempotency_conflict", 409)
            return operation, False
        record = service.store.get("plans", plan_id)
        if record.get("state", "ready") != "ready":
            raise AgentError("plan_not_ready", 409)
        service.ensure_available()
        plan = record["plan"]
        if (
            plan["digest"] != plan_digest
            or plan["generation"] != generation
            or service.state()["inventory"]["generation"] != generation
        ):
            raise AgentError("stale_approval", 409)
        if any(row["binding"]["plan_id"] == plan_id for row in service.store.operations()):
            raise AgentError("plan_already_submitted", 409)
        operation = {
            "id": operation_id,
            "operation": plan["action"],
            "action": plan["action"],
            "state": "queued",
            "stage": "approved",
            "generation": generation,
            "expected_generation": generation,
            "plan": plan,
            "plan_digest": plan_digest,
            "binding": binding,
            "backup": None,
            "release": plan["release"],
            "previous_release": record["previous_release"],
            "error": None,
            "created_at": now(),
            "updated_at": now(),
            "traffic_released": False,
            "events": [{"stage": "approved", "at": now()}],
        }
        service.store.put("operations", operation_id, operation)
    return operation, True
