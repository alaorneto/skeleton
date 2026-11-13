"""Approval-bound durable operations. The journal is authoritative across DB restores."""

import hashlib
import json
import os
import shutil
import threading
import time
from pathlib import Path
from threading import Lock
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4
from zipfile import ZipFile

from packaging.specifiers import SpecifierSet
from packaging.version import Version

from skeleton.system.dependencies import catalog_candidates
from skeleton.system.packages import PackageError, validate_catalog, validate_manifest, validate_package

from .backends import backend_for
from .releases import (
    check_drift,
    check_plan_dependencies,
    create_release,
    file_diff,
    freeze,
    owned_snapshot,
    presentation_diff,
    remove_release,
    verified,
)
from .storage import (
    AgentError,
    Store,
    artifact_id,
    atomic_bytes,
    atomic_json,
    canonical,
    deployment_lock,
    digest,
    file_hash,
    identifier,
    inventory_files,
    load_json,
    log_internal_error,
    now,
    private_file,
)

# First core release whose apps are closed by default and that ships ``upgrade_app_activation`` (#22).
APP_ACTIVATION_UPGRADE_CORE = Version("0.5.0")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        raise AgentError("catalogue_redirect_rejected", 409)


class DeploymentService:
    def __init__(self, config):
        self.config = config
        self.store = Store(config["storage_root"])
        (self.store.root / "probe").mkdir(exist_ok=True, mode=0o700)
        Path(config["releases_root"]).mkdir(parents=True, exist_ok=True, mode=0o755)
        Path(config["maintenance_marker"]).parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        self.backend = backend_for(config, self.store)
        self.worker = None
        self._drift = None
        self._drift_lock = Lock()

    def state(self):
        path = self.store.root / "active.json"
        if not path.exists():
            raise AgentError("deployment_not_initialized", 409)
        return load_json(path)

    def _write_active(self, release, inventory, owned_files):
        """The active release changed: record it and drop the inventory's drift cache (#281)."""
        atomic_json(
            self.store.root / "active.json",
            {"release": str(release), "inventory": inventory, "owned_files": owned_files},
        )
        self._drift = None

    def drift_status(self, state, *, max_age):
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

    def inventory(self):
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

    def upload(self, data):
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

    def initialize(self, inventory_path, *, core_package, app_packages=()):
        """Offline adoption: signed base and an explicit project inventory, never guess a fork."""
        with deployment_lock(self.store.root):
            if (self.store.root / "active.json").exists():
                raise AgentError("already_initialized", 409)
            inventory = load_json(inventory_path)
            if not isinstance(inventory, dict) or not isinstance(inventory.get("apps", []), list):
                raise AgentError("invalid_initial_inventory")
            core = self.upload(Path(core_package).read_bytes())
            if core["manifest"]["kind"] != "core":
                raise AgentError("core_artifact_required")
            for package in app_packages:
                self.upload(Path(package).read_bytes())
            source = Path(self.config["project_root"]).resolve()
            link = Path(self.config["active_link"])
            link.parent.mkdir(parents=True, exist_ok=True)
            if link.exists() or link.is_symlink():
                if not link.is_symlink() or link.resolve() != source:
                    raise AgentError("initial_active_pointer_conflict", 409)
            else:
                link.symlink_to(source, target_is_directory=True)
            # Match a checkout against the signed base; a changed/unknown core is not adoptable.
            core_verified = verified(self.store, self.config, core["digest"])
            with ZipFile(core_verified.path) as payload:
                dependency = next(
                    item
                    for item in core["manifest"]["python_dependencies"]
                    if item["name"].replace("_", "-").lower() == "django-skeleton"
                )
                import io

                with ZipFile(io.BytesIO(payload.read(dependency["wheel"]))) as wheel:
                    files = {
                        name: {"sha256": hashlib.sha256(wheel.read(name)).hexdigest(), "size": len(wheel.read(name))}
                        for name in wheel.namelist()
                        if name.startswith("skeleton/") and not name.endswith("/")
                    }
            if (source / "skeleton").is_dir():
                actual = {f"skeleton/{path}": row for path, row in inventory_files(source / "skeleton").items()}
                if set(actual) != set(files) or any(
                    actual[path]["sha256"] != row["sha256"] for path, row in files.items()
                ):
                    raise AgentError("core_fork_or_unknown_base", 409)
                origin = "source"
            else:
                result_path = self.store.root / "probe" / "adopt-core.json"
                script = "import importlib.metadata,json,pathlib,skeleton,sys; root=pathlib.Path(skeleton.__file__).parent; files={}; import hashlib; "
                script += "files={'skeleton/'+str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'}; "
                script += "pathlib.Path(sys.argv[1]).write_text(json.dumps({'version':importlib.metadata.version('django-skeleton'),'files':files}))"
                self.backend.python(
                    source,
                    ["-c", script, result_path],
                    "export" if self.config["backend"] == "compose" else "runtime",
                    code="core_adoption_failed",
                )
                actual = load_json(result_path)
                result_path.unlink(missing_ok=True)
                if actual["version"] != core["manifest"]["version"] or actual["files"] != {
                    key: row["sha256"] for key, row in files.items()
                }:
                    raise AgentError("core_fork_or_unknown_base", 409)
                origin = "installed"
            entries = []
            identities = {"app_id": set(), "app_label": set(), "package_name": set()}
            for entry in inventory.get("apps", []):
                manifest = validate_manifest(entry["manifest"])
                if manifest["kind"] != "app":
                    raise AgentError("initial_app_artifact_required")
                for key, seen in identities.items():
                    if manifest[key] in seen:
                        raise AgentError("duplicate_initial_component", 409)
                    seen.add(manifest[key])
                state = entry.get("state", "project-local")
                if state not in ("installed", "project-local", "schema_retained"):
                    raise AgentError("invalid_component_state")
                if state == "schema_retained":
                    raise AgentError("adopt_retained_requires_existing_journal")
                if state == "installed":
                    package = verified(self.store, self.config, entry["digest"])
                    if package.manifest != manifest:
                        raise AgentError("initial_app_artifact_conflict")
                    path = source / "applications" / manifest["package_name"]
                    actual_files = inventory_files(path)
                    expected_files = {
                        record["path"].removeprefix("app/"): record
                        for record in manifest["files"]
                        if record["path"].startswith("app/")
                    }
                    if set(actual_files) != set(expected_files) or any(
                        actual_files[name]["sha256"] != record["sha256"]
                        or actual_files[name].get("size") != record["size"]
                        or actual_files[name]["type"] != "file"
                        for name, record in expected_files.items()
                    ):
                        raise AgentError("initial_app_local_drift", 409)
                else:
                    path = Path(entry.get("path", source / "applications" / manifest["package_name"]))
                    if not path.is_absolute():
                        path = source / path
                    if source not in path.resolve().parents:
                        raise AgentError("initial_local_app_outside_project", 409)
                    if not path.is_dir() or path.is_symlink():
                        raise AgentError("initial_local_app_missing", 409)
                    path = path.resolve()
                entries.append(
                    {
                        **entry,
                        "manifest": manifest,
                        "state": state,
                        "path": str(path),
                        "origin": "project-local" if state == "project-local" else "installed",
                    }
                )
            inventory = {
                "generation": 0,
                "core": {
                    "manifest": core["manifest"],
                    "version": core["manifest"]["version"],
                    "origin": origin,
                    "digest": core["digest"],
                    "publisher": core["manifest"]["publisher"],
                    "files": files,
                },
                "apps": entries,
                "retained_schema_paths": [],
                "migration_archives": [],
            }
            atomic_json(source / "inventory.json", inventory, 0o644)
            snapshot = owned_snapshot(source)
            self._write_active(source, inventory, snapshot)
            return inventory

    def ensure_available(self, *, ignore_plan=None):
        if Path(self.config["maintenance_marker"]).exists() or any(
            state in ("queued", "running", "interrupted") for state in self.store.operation_states().values()
        ):
            raise AgentError("recovery_or_operation_pending", 409)
        # One preparation at a time (#298); prepare() ignores its own record.
        if any(
            record.get("state") == "preparing" and plan_id != ignore_plan for plan_id, record, _ in self.store.plans()
        ):
            raise AgentError("recovery_or_operation_pending", 409)

    def plan_status(self, plan_id):
        """Public view of a plan record: never the inventory, owned files or previous_* (#298)."""
        record = self.store.get("plans", identifier(plan_id))
        state = record.get("state", "ready")  # Records written before #298 are ready plans.
        result = {"id": plan_id, "state": state, "created_at": record.get("created_at")}
        if isinstance(record.get("request"), dict):
            result["request"] = {key: record["request"].get(key) for key in ("action", "artifacts", "app_ids")}
        if state == "ready":
            result["plan"] = record["plan"]
            result["created_at"] = result["created_at"] or record["plan"].get("created_at")
        elif state == "failed":
            result["error"] = record.get("error")
            result["details"] = record.get("details")
        return result

    @staticmethod
    def _preview_request(action, artifacts, app_ids):
        """Shape of a preview request: action, artifact digests and app ids (no state read)."""
        artifacts = artifacts or []
        app_ids = app_ids or []
        if (
            action not in ("install", "update", "uninstall", "reinstall", "core_update")
            or not isinstance(artifacts, list)
            or not isinstance(app_ids, list)
            or len(artifacts) > 64
            or len(app_ids) > 64
        ):
            raise AgentError("invalid_preview")
        artifacts = [artifact_id(item) for item in artifacts]
        if (
            len(artifacts) != len(set(artifacts))
            or len(app_ids) != len(set(app_ids))
            or not all(isinstance(value, str) for value in app_ids)
        ):
            raise AgentError("duplicate_or_invalid_components")
        if action == "uninstall" and (not app_ids or artifacts) or action != "uninstall" and (not artifacts or app_ids):
            raise AgentError("invalid_action_targets")
        return artifacts, app_ids

    def _check_preview(self, action, artifacts, app_ids, *, ignore_plan=None):
        """Checks that need the journal and the active release; the caller holds deployment_lock.

        Dependency problems leave here with their details, synchronously in preview (#298).
        """
        self.ensure_available(ignore_plan=ignore_plan)
        state = self.state()
        source = self.backend.active()
        if str(source) != state["release"]:
            raise AgentError("active_pointer_conflict", 409)
        check_drift(source, state["owned_files"])
        self.backend.preflight(source)  # The operation would fail after maintenance otherwise (#246).
        inventory = state["inventory"]
        old_apps = {entry["manifest"]["app_id"]: entry for entry in inventory["apps"]}
        kinds = []
        manifests = []
        for value in artifacts:
            manifest = verified(self.store, self.config, value).manifest
            manifests.append(manifest)
            kind = manifest["kind"]
            component = manifest.get("app_id", "django-skeleton")
            kinds.append(component)
            existing = old_apps.get(component)
            if action == "install" and kind == "app" and existing:
                raise AgentError("already_installed_or_wrong_action", 409)
            if action == "reinstall" and (not existing or existing["state"] != "schema_retained"):
                raise AgentError("not_retained", 409)
            if action in ("update", "core_update") and existing and existing["state"] == "schema_retained":
                raise AgentError("explicit_reinstall_required", 409)
        if len(kinds) != len(set(kinds)) or action == "core_update" and "django-skeleton" not in kinds:
            raise AgentError("duplicate_component_or_missing_core")
        # An update may bring new dependencies along, but must update something installed.
        if action == "update" and not any(component in old_apps for component in kinds):
            raise AgentError("not_installed_use_install", 409)
        # Resolve the resulting component set before any copy, extraction or lock work.
        check_plan_dependencies(
            self.config, self.backend, inventory, manifests, app_ids, suggest=self.catalogue_suggestions
        )
        return state, source, inventory

    def preview(self, action, artifacts=None, app_ids=None):
        """Validate quickly, record the plan as ``preparing`` and build it in a thread (#298).

        The web request no longer waits for the release copy, pip, checks and image build:
        it receives ``{id, state}`` and follows ``GET /plans/<id>``.
        """
        artifacts, app_ids = self._preview_request(action, artifacts, app_ids)
        with deployment_lock(self.store.root):
            state, _, _ = self._check_preview(action, artifacts, app_ids)
            plan_id = str(uuid4())
            self.store.put(
                "plans",
                plan_id,
                {
                    "state": "preparing",
                    "request": {"action": action, "artifacts": artifacts, "app_ids": app_ids},
                    "generation": state["inventory"]["generation"],
                    "created_at": now(),
                    "error": None,
                    "details": None,
                },
            )
        # The lock is released before the thread starts; prepare() takes it again and rechecks.
        self.worker = threading.Thread(target=self.prepare, args=(plan_id,), daemon=False, name=f"preview-{plan_id}")
        self.worker.start()
        return {"id": plan_id, "state": "preparing"}

    def prepare(self, plan_id):
        """Build the release and the plan of a ``preparing`` record; ``ready`` or ``failed`` at the end."""
        with deployment_lock(self.store.root, blocking=True):
            try:
                request_record = self.store.get("plans", plan_id)
            except AgentError:
                return None
            if request_record.get("state", "ready") != "preparing":
                return None
            request_record = {key: request_record.get(key) for key in ("request", "generation", "created_at")}
            action = request_record["request"]["action"]
            artifacts, app_ids = request_record["request"]["artifacts"], request_record["request"]["app_ids"]
            try:
                # The lock was released between preview() and here: everything is checked again.
                state, source, inventory = self._check_preview(action, artifacts, app_ids, ignore_plan=plan_id)
                if inventory["generation"] != request_record["generation"]:
                    raise AgentError("generation_changed", 409)
                self.backend.manage(source, ["check_skeleton_schema"], "runtime", code="current_schema_invalid")
                required = self.config.get("minimum_free_bytes", 512 * 1024 * 1024)
                if shutil.disk_usage(self.config["releases_root"]).free < required:
                    raise AgentError("insufficient_disk_space", 409)
            except Exception as error:  # noqa: BLE001 - any failure marks the plan failed with its code or class
                return self._preparation_failed(plan_id, request_record, error)
            release = Path(self.config["releases_root"]) / f"{inventory['generation'] + 1}-{plan_id}"
            try:
                target = create_release(self.config, self.store, self.backend, inventory, artifacts, app_ids, release)
                graph = self.backend.probe(release)
                infrastructure = release / "infrastructure.json"
                self.backend.export_infrastructure(release, infrastructure)
                target["frozen_infrastructure"] = {"path": str(infrastructure), "sha256": file_hash(infrastructure)}
                atomic_json(release / "inventory.json", target, 0o644)
                if self.config["backend"] == "compose":
                    # Seal the selected image with final static and schema metadata,
                    # not merely the provisional payload needed to run preflight.
                    self.backend.build_image(release)
                owned = owned_snapshot(release)
                atomic_json(release / "owned-files.json", owned, 0o644)
                components = [
                    {
                        "component_id": "django-skeleton",
                        **{key: target["core"][key] for key in ("version", "digest", "publisher")},
                    }
                ]
                components += [
                    {
                        "component_id": entry["manifest"]["app_id"],
                        "version": entry["manifest"]["version"],
                        "digest": entry.get("digest"),
                        "state": entry["state"],
                        "publisher": entry["manifest"]["publisher"],
                    }
                    for entry in target["apps"]
                ]
                retained_impact = [
                    load_json(reference["path"]).get("impact", {}) for reference in target["retained_schema_paths"]
                ]
                plan = {
                    "id": plan_id,
                    "generation": inventory["generation"],
                    "action": action,
                    "components": components,
                    "files": file_diff(state["owned_files"], owned),
                    "migrations": graph["migrations"],
                    "applied_migrations": graph["applied"],
                    "migration_targets": graph["targets"],
                    "impact": {**graph["impact"], "retained_schema": retained_impact},
                    "maintenance": True,
                    "backup_required": True,
                    "recovery": "restore_database_and_previous_release_before_new_writes; operator_repair_to_complete",
                    "release": str(release),
                    "release_digest": digest(owned),
                    "artifacts": artifacts,
                    "app_ids": app_ids,
                    "created_at": now(),
                    "backend": self.config["backend"],
                }
                plan["presentation_changes"] = presentation_diff(inventory, target)
                plan["resources"] = [
                    {
                        "resource": self.config["active_link"],
                        "owner": "deployment",
                        "action": "replace_symlink",
                        "before": str(source),
                        "after": str(release),
                    },
                    {
                        "resource": "database",
                        "owner": "deployment",
                        "action": "migrate",
                        "before": digest(graph["applied"]),
                        "after": digest(graph["targets"]),
                    },
                    {"resource": "maintenance", "owner": "deployment", "action": "gate_and_drain"},
                ]
                if self.config["backend"] == "compose":
                    plan["resources"].append(
                        {
                            "resource": "compose_image",
                            "owner": "deployment",
                            "action": "replace",
                            "before": self.backend.image(source),
                            "after": self.backend.image(release),
                        }
                    )
                plan["digest"] = digest(plan)
                self.store.put(
                    "plans",
                    plan_id,
                    {
                        **request_record,
                        "state": "ready",
                        "plan": plan,
                        "inventory": target,
                        "owned_files": owned,
                        "source_owned_digest": digest(state["owned_files"]),
                        "previous_release": str(source),
                        "previous_inventory": inventory,
                        "previous_owned_files": state["owned_files"],
                    },
                )
                freeze(release)
                return plan
            except Exception as error:  # noqa: BLE001 - any failure removes the partial release and fails the plan
                if release.exists():
                    # Preparation has not touched live state; retain no unsigned/partial executable release.
                    remove_release(release)
                return self._preparation_failed(plan_id, request_record, error)

    def _preparation_failed(self, plan_id, request_record, error):
        """Record the error code (AgentError) or the class name; the message never leaves the agent."""
        if not isinstance(error, AgentError):
            log_internal_error(error)
        details = getattr(error, "details", None)
        self.store.put(
            "plans",
            plan_id,
            {
                **request_record,
                "state": "failed",
                "error": getattr(error, "code", None) or type(error).__name__,
                "details": details if details else None,
            },
        )

    def submit(self, plan_id, plan_digest, generation, operation_id, actor_id, request_id):
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
        path = self.store.path("operations", operation_id)
        if path.exists():
            operation = load_json(path)
            if operation["binding"] != binding:
                raise AgentError("idempotency_conflict", 409)
            return self.public_operation(operation)
        with deployment_lock(self.store.root):
            # Recheck after locking: another submit may have journaled this UUID.
            if path.exists():
                operation = load_json(path)
                if operation["binding"] != binding:
                    raise AgentError("idempotency_conflict", 409)
                return self.public_operation(operation)
            record = self.store.get("plans", plan_id)
            if record.get("state", "ready") != "ready":
                raise AgentError("plan_not_ready", 409)
            self.ensure_available()
            plan = record["plan"]
            if (
                plan["digest"] != plan_digest
                or plan["generation"] != generation
                or self.state()["inventory"]["generation"] != generation
            ):
                raise AgentError("stale_approval", 409)
            if any(row["binding"]["plan_id"] == plan_id for row in self.store.operations()):
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
            self.store.put("operations", operation_id, operation)
        self.worker = threading.Thread(
            target=self.execute, args=(operation_id,), daemon=False, name=f"deployment-{operation_id}"
        )
        self.worker.start()
        return self.public_operation(operation)

    def public_operation(self, operation, include_plan=True):
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

    def operation(self, operation_id):
        return self.public_operation(self.store.get("operations", identifier(operation_id)))

    def stage(self, operation, stage, *, log=None, **changes):
        operation.update(changes)
        operation["stage"] = stage
        operation["updated_at"] = now()
        event = {"stage": stage, "at": operation["updated_at"]}
        if log:
            event["log"] = log  # Only the file name under logs/<operation>/ (#244), never its content.
        operation["events"].append(event)
        self.store.put("operations", operation["id"], operation)

    def maintain(self, operation):
        atomic_json(
            self.config["maintenance_marker"], {"operation": operation["id"], "stage": operation["stage"]}, 0o644
        )

    def execute(self, operation_id):
        with deployment_lock(self.store.root, blocking=True):
            operation = self.store.get("operations", operation_id)
            if operation["state"] != "queued":
                return
            with self.backend.logging_to(operation_id):
                try:
                    record = self.store.get("plans", operation["binding"]["plan_id"])
                    plan = record["plan"]
                    state = self.state()
                    if (
                        state["inventory"]["generation"] != plan["generation"]
                        or state["release"] != record["previous_release"]
                        or digest(state["owned_files"]) != record["source_owned_digest"]
                    ):
                        raise AgentError("stale_execution", 409)
                    if str(self.backend.active()) != state["release"]:
                        raise AgentError("active_pointer_conflict", 409)
                    if shutil.disk_usage(self.config["releases_root"]).free < self.config.get(
                        "minimum_free_bytes", 512 * 1024 * 1024
                    ):
                        raise AgentError("insufficient_disk_space", 409)
                    check_drift(state["release"], state["owned_files"])
                    check_drift(plan["release"], record["owned_files"])
                    for artifact in plan["artifacts"]:
                        verified(self.store, self.config, artifact)
                    graph = self.backend.probe(plan["release"])
                    if (
                        graph["migrations"] != plan["migrations"]
                        or graph["applied"] != plan["applied_migrations"]
                        or graph["targets"] != plan["migration_targets"]
                    ):
                        raise AgentError("migration_plan_changed", 409)
                    # Configuration checked before any effect: no marker, no drain on a typo (#246).
                    self.backend.preflight(state["release"])
                    self.stage(operation, "maintenance_entering", state="running")
                    self.maintain(operation)
                    self.backend.drain(state["release"], operation_id)
                    self.stage(operation, "drained")
                    backup = self.backend.backup(state["release"], operation_id)
                    self.stage(operation, "backup_ready", backup=backup)
                    # Recheck under the write gate; no DB changes may escape approval between preview and drain.
                    graph = self.backend.probe(plan["release"])
                    if (
                        graph["migrations"] != plan["migrations"]
                        or graph["applied"] != plan["applied_migrations"]
                        or graph["targets"] != plan["migration_targets"]
                    ):
                        raise AgentError("migration_plan_changed", 409)
                    self.stage(operation, "migration_started")
                    self.finish(operation, record)
                except Exception as error:  # noqa: BLE001 - any failure is journaled before re-raising
                    if not isinstance(error, AgentError):
                        log_internal_error(error)  # Class and stack only, never the message (#244).
                    code = error.code if isinstance(error, AgentError) else type(error).__name__
                    self.stage(operation, "failed", state="failed", error=code, log=getattr(error, "log", None))
                    # Once the gate exists, no failure clears it or silently rolls files back across a schema change.

    def finish(self, operation, record):
        release = Path(operation["release"])
        self.backend.manage(release, ["migrate", "--noinput"], "migration", code="migration_failed")
        self.stage(operation, "migrated")
        self.backend.manage(release, ["sync_table_catalog"], "migration", code="catalogue_sync_failed")
        if Version(record["inventory"]["core"]["version"]) >= APP_ACTIVATION_UPGRADE_CORE:
            # Closed-by-default activation (#22): keep what organizations used before; idempotent per run.
            self.backend.manage(release, ["upgrade_app_activation"], "runtime", code="app_activation_upgrade_failed")
        self.backend.manage(release, ["check_skeleton_schema"], "runtime", code="runtime_schema_failed")
        if Version(record["inventory"]["core"]["version"]) >= Version("0.2.0"):
            self.backend.manage(
                release, ["check_application_configuration"], "runtime", code="application_configuration_invalid"
            )
        self.backend.manage(
            release, ["validate_provider_bindings", "--database", "default"], "runtime", code="provider_bindings_failed"
        )
        self.backend.manage(release, ["check", "--deploy"], "runtime", code="deployment_checks_failed")
        self.stage(operation, "runtime_checked")
        check_drift(release, record["owned_files"])
        self.stage(operation, "cutover_started")
        self.backend.cutover(release)
        self.stage(operation, "release_switched")
        self.backend.start(release, operation["id"])
        self.stage(operation, "workers_started")
        self.health(record["inventory"])
        self.stage(operation, "health_verified", generation=record["inventory"]["generation"])
        self.backend.manage(
            release,
            [
                "sync_deployment_state",
                "--inventory",
                release / "inventory.json",
                "--operation",
                self.store.path("operations", operation["id"]),
            ],
            "migration",
            code="state_sync_failed",
        )
        self._write_active(release, record["inventory"], record["owned_files"])
        self.stage(operation, "committed", state="succeeded")
        self.backend.manage(
            release,
            [
                "sync_deployment_state",
                "--inventory",
                release / "inventory.json",
                "--operation",
                self.store.path("operations", operation["id"]),
            ],
            "migration",
            code="state_sync_failed",
        )
        # Journal write intent precedes traffic release; an interrupted release is conservatively post-writes.
        self.stage(operation, "traffic_release_started", traffic_released=True)
        Path(self.config["maintenance_marker"]).unlink(missing_ok=True)
        self.stage(operation, "ready", state="succeeded")

    def health(self, inventory):
        opener = build_opener(NoRedirect())
        deadline = time.monotonic() + self.config.get("health_timeout", 60)
        urls = self.config["health_urls"]
        release_url = self.config.get("release_health_url", urljoin(urls[0], "/health/release/"))
        last = "health_failed"
        while time.monotonic() < deadline:
            try:
                headers = {"Host": self.config["health_host"]} if self.config.get("health_host") else {}
                if self.config.get("smoke_token_file"):
                    token = private_file(self.config["smoke_token_file"])  # Same rule as the control token.
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

    def reconcile_interrupted(self):
        with deployment_lock(self.store.root):
            for operation in self.store.operations():
                if operation["state"] in ("queued", "running") or operation["stage"] in (
                    "committed",
                    "traffic_release_started",
                ):
                    self.stage(operation, "interrupted", state="interrupted", error="agent_interrupted")
                    self.maintain(operation)
            for plan_id, record, _ in self.store.plans():
                if record.get("state") != "preparing":
                    continue
                # Preparation never touches the active release: no maintenance marker (#298).
                root = Path(self.config["releases_root"])
                for release in root.glob(f"*-{plan_id}") if root.is_dir() else ():
                    if release.is_dir() and not release.is_symlink():
                        remove_release(release)
                self.store.put("plans", plan_id, {**record, "state": "failed", "error": "agent_interrupted"})
            state_path = self.store.root / "active.json"
            if state_path.exists():
                state = load_json(state_path)
                try:
                    if str(self.backend.active()) != state["release"]:
                        raise AgentError("active_pointer_conflict")
                    check_drift(state["release"], state["owned_files"])
                except AgentError:
                    atomic_json(self.config["maintenance_marker"], {"stage": "active_state_conflict"}, 0o644)

    @staticmethod
    def _recoverable(operation, record, state):
        """The active (release, generation) is still the one before or after this operation."""
        allowed_states = {
            record["previous_release"]: record["plan"]["generation"],
            operation["release"]: record["inventory"]["generation"],
        }
        return state["inventory"]["generation"] == allowed_states.get(state["release"])

    def prune(self, *, plan_days=7, backup_days=None, apply=False):
        """List (and with apply, remove) what no operation can still need (#245). Offline CLI only.

        Never removed: the active release, ``release``/``previous_release`` and backup of a failed
        operation that ``recover`` would still accept, the previous release of the last success,
        the project root, the journal and the uploaded artifacts. Backups only with backup_days.
        """
        with deployment_lock(self.store.root):
            self.ensure_available()
            state = self.state()
            operations = self.store.operations()  # Newest first.
            root = Path(self.config["releases_root"]).resolve()
            now_time = time.time()
            protected = {Path(state["release"]).resolve()} if state.get("release") else set()
            if self.config.get("project_root"):
                protected.add(Path(self.config["project_root"]).resolve())
            recoverable = set()
            for operation in operations:
                if operation["state"] != "failed":
                    continue
                try:
                    record = self.store.get("plans", operation["binding"]["plan_id"])
                except AgentError:
                    continue
                if self._recoverable(operation, record, state):
                    recoverable.add(operation["id"])
                    protected |= {Path(operation["release"]).resolve(), Path(operation["previous_release"]).resolve()}
            last_success = next((row for row in operations if row["state"] == "succeeded"), None)
            if last_success:
                protected |= {Path(last_success["release"]).resolve(), Path(last_success["previous_release"]).resolve()}
            result = {
                "apply": apply,
                "releases": [],
                "plans": [],
                "images": [],
                "backups": [],
                "skipped": [],
                "errors": [],
            }
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
            for plan_id, record, mtime in self.store.plans():
                if plan_id in submitted or now_time - mtime < plan_days * 86400:
                    continue
                result["plans"].append(plan_id)
                release = (record.get("plan") or {}).get("release")
                if release:
                    candidate_release(release, "unsubmitted_preview")
            finished = [row for row in operations[1:] if row["state"] in ("succeeded", "restored", "cancelled")]
            for operation in finished:
                candidate_release(operation["release"], f"operation_{operation['state']}")
            if backup_days is not None:
                finished_ids = {
                    row["id"] for row in operations if row["state"] in ("succeeded", "restored", "cancelled")
                }
                for folder in ("backups", "logs"):
                    for path in sorted((self.store.root / folder).glob("*")):
                        owner = path.name[:36]
                        if (
                            path.is_dir()
                            and not path.is_symlink()
                            and owner in finished_ids
                            and owner not in recoverable
                            and now_time - path.stat().st_mtime >= backup_days * 86400
                        ):
                            result["backups"].append(str(path))
            image = getattr(self.backend, "image", None)
            result["images"] = [image(path) for path in releases] if image else []
            if not apply:
                return result
            for path in releases:
                remove_release(path)
            for plan_id in result["plans"]:
                self.store.path("plans", plan_id).unlink(missing_ok=True)
            for path in result["backups"]:
                shutil.rmtree(path)
            for tag in result["images"]:
                try:
                    self.backend.run(
                        [self.config.get("docker", "/usr/bin/docker"), "image", "rm", tag], code="image_remove_failed"
                    )
                except AgentError as error:
                    result["errors"].append({"image": tag, "code": error.code})
            return result

    def recover(self, operation_id, action, *, acknowledge_new_writes=False):
        with deployment_lock(self.store.root):
            operation = self.store.get("operations", identifier(operation_id))
            if action not in ("restore", "complete", "abort"):
                raise AgentError("invalid_recovery_action")
            if operation["state"] not in ("failed", "interrupted"):
                raise AgentError("operation_not_recoverable", 409)
            record = self.store.get("plans", operation["binding"]["plan_id"])
            state = self.state()
            if not self._recoverable(operation, record, state):
                raise AgentError("operation_superseded", 409)
            if any(
                row["id"] != operation["id"] and row["state"] in ("queued", "running", "interrupted")
                for row in self.store.operations()
            ):
                raise AgentError("recovery_or_operation_pending", 409)
            with self.backend.logging_to(operation["id"]):
                self.maintain(operation)
                self.backend.drain(self.backend.active(), operation["id"])
                try:
                    if action == "complete":
                        check_drift(operation["release"], record["owned_files"])
                        actual = self.backend.probe(operation["release"])
                        approved = {(row["app_label"], row["name"]): row for row in operation["plan"]["migrations"]}
                        if (
                            any(approved.get((row["app_label"], row["name"])) != row for row in actual["migrations"])
                            or actual["targets"] != operation["plan"]["migration_targets"]
                        ):
                            raise AgentError("recovery_migration_plan_changed", 409)
                        if not operation["backup"]:
                            operation["backup"] = self.backend.backup(operation["previous_release"], operation["id"])
                        self.stage(operation, "operator_completion", state="running", error=None)
                        self.finish(operation, record)
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
                            self.stage(operation, "restore_started", state="running")
                            self.backend.restore(operation["backup"], previous)
                        previous_inventory = record["previous_inventory"]
                        previous_owned = record["previous_owned_files"]
                        if action == "restore":
                            self.backend.manage(
                                previous,
                                ["reinstall_record_infrastructure", "--database", "default"],
                                "migration",
                                code="restored_infrastructure_failed",
                            )
                        check_drift(previous, previous_owned)
                        self.backend.manage(
                            previous, ["check_skeleton_schema"], "runtime", code="restored_schema_failed"
                        )
                        self.backend.manage(
                            previous,
                            ["validate_provider_bindings", "--database", "default"],
                            "runtime",
                            code="restored_bindings_failed",
                        )
                        self.backend.manage(previous, ["check", "--deploy"], "runtime", code="restored_checks_failed")
                        self.backend.cutover(previous)
                        self.backend.start(previous, operation["id"])
                        self.health(previous_inventory)
                        self._write_active(previous, previous_inventory, previous_owned)
                        self.stage(
                            operation,
                            "restored" if action == "restore" else "aborted",
                            state="restored" if action == "restore" else "cancelled",
                            error=None,
                            generation=previous_inventory["generation"],
                        )
                        self.backend.manage(
                            previous,
                            [
                                "sync_deployment_state",
                                "--inventory",
                                previous / "inventory.json",
                                "--operation",
                                self.store.path("operations", operation["id"]),
                            ],
                            "migration",
                            code="state_sync_failed",
                        )
                        Path(self.config["maintenance_marker"]).unlink(missing_ok=True)
                    return self.public_operation(operation)
                except Exception as error:
                    if not isinstance(error, AgentError):
                        log_internal_error(error)
                    self.stage(
                        operation,
                        "recovery_failed",
                        state="failed",
                        error=error.code if isinstance(error, AgentError) else type(error).__name__,
                        log=getattr(error, "log", None),
                    )
                    raise

    def catalogue_suggestions(self, dependency, required, target, *, limit=3):
        """Compatible releases from already verified catalogue caches; never downloads anything."""
        suggestions = []
        for source in self.catalogue_sources():
            cache = self.store.root / "catalogues" / f"{hashlib.sha256(source['id'].encode()).hexdigest()}.json"
            if not cache.exists():
                continue
            try:
                saved = load_json(cache)
                body = validate_catalog(
                    saved["envelope"],
                    self.config["approved_publishers"],
                    minimum_generation=saved["generation"],
                    channels=source.get("channels", ["stable"]),
                )
            except (PackageError, KeyError, OSError, ValueError):
                continue
            for entry in catalog_candidates(body["entries"], dependency, required, target, limit=limit):
                suggestions.append(
                    {
                        "source_id": source["id"],
                        "component_id": entry["component_id"],
                        "version": entry["version"],
                        "digest": entry["digest"],
                        # None marks a v1 catalogue: ranges unknown until the artifact is verified.
                        "dependencies": entry.get("dependencies"),
                    }
                )
        suggestions.sort(key=lambda item: Version(item["version"]), reverse=True)
        return suggestions[:limit]

    def catalogue_sources(self):
        sources = self.config.get("catalogue_sources", [])
        if not isinstance(sources, list) or any(
            not isinstance(source, dict) or not isinstance(source.get("id"), str) for source in sources
        ):
            raise AgentError("invalid_catalogue_sources")
        return sources

    def fetch_bytes(self, location, maximum, *, origin=None):
        parts = urlsplit(location)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.fragment:
            raise AgentError("unapproved_catalogue_url")
        if origin is not None and (parts.scheme, parts.hostname, parts.port) != origin:
            raise AgentError("catalogue_artifact_off_origin")
        try:
            with build_opener(NoRedirect()).open(
                Request(location, headers={"Accept": "application/json, application/octet-stream"}),  # noqa: S310 - fetch_bytes accepts https only, checked above
                timeout=self.config.get("catalogue_timeout", 20),
            ) as response:
                content = response.read(maximum + 1)
                if len(content) > maximum:
                    raise AgentError("catalogue_download_too_large", 413)
                return content
        except AgentError:
            raise
        except Exception:  # noqa: BLE001 - any transport failure is catalogue_unavailable
            raise AgentError("catalogue_unavailable", 409) from None

    def check_updates(self):
        result = {"checked_at": now(), "sources": [], "releases": []}
        inventory = self.state()["inventory"]
        installed = {
            entry["manifest"]["app_id"]: entry["manifest"]["version"]
            for entry in inventory["apps"]
            if entry["state"] != "schema_retained"
        }
        installed["django-skeleton"] = inventory["core"]["version"]
        for source in self.catalogue_sources():
            entry = {"id": source["id"], "state": "available", "error_code": None}
            try:
                cache = self.store.root / "catalogues" / f"{hashlib.sha256(source['id'].encode()).hexdigest()}.json"
                previous = load_json(cache) if cache.exists() else {"generation": 0}
                if source.get("url"):
                    content = self.fetch_bytes(source["url"], 4 * 1024 * 1024)
                elif source.get("path") and Path(source["path"]).is_absolute():
                    if Path(source["path"]).stat().st_size > 4 * 1024 * 1024:
                        raise AgentError("catalogue_download_too_large")
                    content = Path(source["path"]).read_bytes()
                else:
                    raise AgentError("invalid_catalogue_source")
                envelope = json.loads(content)
                body = validate_catalog(
                    envelope,
                    self.config["approved_publishers"],
                    minimum_generation=previous["generation"],
                    channels=source.get("channels", ["stable"]),
                )
                if (
                    previous.get("generation") == body["generation"]
                    and previous.get("digest")
                    and previous["digest"] != digest(envelope)
                ):
                    raise AgentError("catalogue_generation_content_changed")
                atomic_json(
                    cache,
                    {
                        "generation": body["generation"],
                        "digest": digest(envelope),
                        "envelope": envelope,
                        "checked_at": result["checked_at"],
                    },
                )
                import platform

                # Prefer the target runtime recorded by the active release over the agent's own.
                runtime = inventory.get("runtime") or {}
                python_version = runtime.get("python") or platform.python_version()
                django_version = runtime.get("django") or self.config.get("django_version", "6.1.3")
                for release in body["entries"]:
                    compatible = all(
                        Version(value) in SpecifierSet(release["compatibility"][key])
                        for key, value in (
                            ("skeleton", inventory["core"]["version"]),
                            ("python", python_version),
                            ("django", django_version),
                        )
                    )
                    current = installed.get(release["component_id"])
                    state = (
                        "incompatible"
                        if not compatible
                        else "current"
                        if current and Version(release["version"]) <= Version(current)
                        else "available"
                    )
                    result["releases"].append(
                        {
                            "source_id": source["id"],
                            "component_id": release["component_id"],
                            "version": release["version"],
                            "state": state,
                            "notes": release["notes"],
                            "channel": release["channel"],
                            "digest": release["digest"],
                            "compatibility": release["compatibility"],
                        }
                    )
            except Exception as error:  # noqa: BLE001 - one unverifiable source never hides the others
                entry.update(
                    state="unavailable",
                    error_code=error.code if isinstance(error, AgentError) else "catalogue_verification_failed",
                )
            result["sources"].append(entry)
        atomic_json(self.store.root / "catalogue-status.json", result)
        return result

    def fetch_release(self, component_id, version, source_id):
        sources = [source for source in self.catalogue_sources() if source["id"] == source_id]
        if len(sources) != 1 or not isinstance(component_id, str) or not isinstance(version, str):
            raise AgentError("invalid_catalogue_selection")
        source = sources[0]
        cache = self.store.root / "catalogues" / f"{hashlib.sha256(source_id.encode()).hexdigest()}.json"
        if not cache.exists():
            self.check_updates()
        saved = load_json(cache)
        try:
            body = validate_catalog(
                saved["envelope"],
                self.config["approved_publishers"],
                minimum_generation=saved["generation"],
                channels=source.get("channels", ["stable"]),
            )
        except PackageError:
            raise AgentError("catalogue_verification_failed", 409) from None
        entries = [
            entry for entry in body["entries"] if entry["component_id"] == component_id and entry["version"] == version
        ]
        if len(entries) != 1:
            raise AgentError("catalogue_release_not_unique", 409)
        entry = entries[0]
        if source.get("url"):
            parts = urlsplit(source["url"])
            content = self.fetch_bytes(
                urljoin(source["url"], entry["artifact"]),
                self.config.get("max_upload_bytes", 128 * 1024 * 1024),
                origin=(parts.scheme, parts.hostname, parts.port),
            )
        else:
            base = Path(source["path"]).parent.resolve()
            target = (base / entry["artifact"]).resolve()
            if (
                target == base
                or base not in target.parents
                or not target.is_file()
                or target.stat().st_size > self.config.get("max_upload_bytes", 128 * 1024 * 1024)
            ):
                raise AgentError("catalogue_artifact_path_rejected")
            content = target.read_bytes()
        if hashlib.sha256(content).hexdigest() != entry["digest"]:
            raise AgentError("catalogue_artifact_hash_conflict", 409)
        result = self.upload(content)
        manifest = result["manifest"]
        if (
            manifest.get("app_id", manifest.get("component_id")) != component_id
            or manifest["version"] != version
            or manifest["publisher"] != entry["publisher"]
        ):
            raise AgentError("catalogue_artifact_identity_conflict", 409)
        with ZipFile(self.store.path("uploads", result["digest"])) as archive:
            if archive.read("manifest.sig").decode("ascii") != entry["signature"]:
                raise AgentError("catalogue_artifact_signature_conflict", 409)
        return result
