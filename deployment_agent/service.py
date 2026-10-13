"""Approval-bound durable operations. The journal is authoritative across DB restores."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
import time
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4
from zipfile import ZipFile

from packaging.specifiers import SpecifierSet
from packaging.version import Version
from skeleton.system.packages import PackageError, validate_catalog, validate_manifest, validate_package

from .backends import backend_for
from .releases import check_drift, create_release, file_diff, freeze, owned_snapshot, verified, wheel_files
from .storage import AgentError, Store, artifact_id, atomic_bytes, atomic_json, canonical, deployment_lock, digest, file_hash, identifier, inventory_files, load_json, now


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

    def state(self):
        path = self.store.root / "active.json"
        if not path.exists():
            raise AgentError("deployment_not_initialized", 409)
        return load_json(path)

    def inventory(self):
        state = self.state()
        result = json.loads(canonical(state["inventory"]))
        result["operations"] = [self.public_operation(row, include_plan=False) for row in self.store.operations()[:50]]
        result["maintenance"] = Path(self.config["maintenance_marker"]).exists()
        try:
            if str(self.backend.active()) != state["release"]:
                result["error"] = "active_pointer_conflict"
            else:
                check_drift(state["release"], state["owned_files"])
        except AgentError as error:
            result["error"] = error.code
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
                dependency = next(item for item in core["manifest"]["python_dependencies"] if item["name"].replace("_", "-").lower() == "django-skeleton")
                import io
                with ZipFile(io.BytesIO(payload.read(dependency["wheel"]))) as wheel:
                    files = {name: {"sha256": hashlib.sha256(wheel.read(name)).hexdigest(), "size": len(wheel.read(name))} for name in wheel.namelist() if name.startswith("skeleton/") and not name.endswith("/")}
            if (source / "skeleton").is_dir():
                actual = {f"skeleton/{path}": row for path, row in inventory_files(source / "skeleton").items()}
                if set(actual) != set(files) or any(actual[path]["sha256"] != row["sha256"] for path, row in files.items()):
                    raise AgentError("core_fork_or_unknown_base", 409)
                origin = "source"
            else:
                result_path = self.store.root / "probe" / "adopt-core.json"
                script = "import importlib.metadata,json,pathlib,skeleton,sys; root=pathlib.Path(skeleton.__file__).parent; files={}; import hashlib; "
                script += "files={'skeleton/'+str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'}; "
                script += "pathlib.Path(sys.argv[1]).write_text(json.dumps({'version':importlib.metadata.version('django-skeleton'),'files':files}))"
                self.backend.python(source, ["-c", script, result_path], "export" if self.config["backend"] == "compose" else "runtime", code="core_adoption_failed")
                actual = load_json(result_path)
                result_path.unlink(missing_ok=True)
                if actual["version"] != core["manifest"]["version"] or actual["files"] != {key: row["sha256"] for key, row in files.items()}:
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
                    expected_files = {record["path"].removeprefix("app/"): record for record in manifest["files"] if record["path"].startswith("app/")}
                    if set(actual_files) != set(expected_files) or any(actual_files[name]["sha256"] != record["sha256"] or actual_files[name].get("size") != record["size"] or actual_files[name]["type"] != "file" for name, record in expected_files.items()):
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
                entries.append({**entry, "manifest": manifest, "state": state, "path": str(path), "origin": "project-local" if state == "project-local" else "installed"})
            inventory = {"generation": 0, "core": {"manifest": core["manifest"], "version": core["manifest"]["version"], "origin": origin, "digest": core["digest"], "publisher": core["manifest"]["publisher"], "files": files}, "apps": entries, "retained_schema_paths": [], "migration_archives": []}
            atomic_json(source / "inventory.json", inventory, 0o644)
            snapshot = owned_snapshot(source)
            atomic_json(self.store.root / "active.json", {"release": str(source), "inventory": inventory, "owned_files": snapshot})
            return inventory

    def ensure_available(self):
        if Path(self.config["maintenance_marker"]).exists() or any(row["state"] in ("queued", "running", "interrupted") for row in self.store.operations()):
            raise AgentError("recovery_or_operation_pending", 409)

    def preview(self, action, artifacts=None, app_ids=None):
        artifacts = artifacts or []
        app_ids = app_ids or []
        if action not in ("install", "update", "uninstall", "reinstall", "core_update") or not isinstance(artifacts, list) or not isinstance(app_ids, list) or len(artifacts) > 64 or len(app_ids) > 64:
            raise AgentError("invalid_preview")
        artifacts = [artifact_id(item) for item in artifacts]
        if len(artifacts) != len(set(artifacts)) or len(app_ids) != len(set(app_ids)) or not all(isinstance(value, str) for value in app_ids):
            raise AgentError("duplicate_or_invalid_components")
        if action == "uninstall" and (not app_ids or artifacts) or action != "uninstall" and (not artifacts or app_ids):
            raise AgentError("invalid_action_targets")
        with deployment_lock(self.store.root):
            self.ensure_available()
            state = self.state()
            source = self.backend.active()
            if str(source) != state["release"]:
                raise AgentError("active_pointer_conflict", 409)
            check_drift(source, state["owned_files"])
            inventory = state["inventory"]
            old_apps = {entry["manifest"]["app_id"]: entry for entry in inventory["apps"]}
            kinds = []
            for value in artifacts:
                manifest = verified(self.store, self.config, value).manifest
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
            self.backend.manage(source, ["check_skeleton_schema"], "runtime", code="current_schema_invalid")
            required = self.config.get("minimum_free_bytes", 512 * 1024 * 1024)
            if shutil.disk_usage(self.config["releases_root"]).free < required:
                raise AgentError("insufficient_disk_space", 409)
            plan_id = str(uuid4())
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
                components = [{"component_id": "django-skeleton", **{key: target["core"][key] for key in ("version", "digest", "publisher")}}]
                components += [{"component_id": entry["manifest"]["app_id"], "version": entry["manifest"]["version"], "digest": entry.get("digest"), "state": entry["state"], "publisher": entry["manifest"]["publisher"]} for entry in target["apps"]]
                retained_impact = [load_json(reference["path"]).get("impact", {}) for reference in target["retained_schema_paths"]]
                plan = {"id": plan_id, "generation": inventory["generation"], "action": action, "components": components, "files": file_diff(state["owned_files"], owned), "migrations": graph["migrations"], "applied_migrations": graph["applied"], "migration_targets": graph["targets"], "impact": {**graph["impact"], "retained_schema": retained_impact}, "maintenance": True, "backup_required": True, "recovery": "restore_database_and_previous_release_before_new_writes; operator_repair_to_complete", "release": str(release), "release_digest": digest(owned), "artifacts": artifacts, "app_ids": app_ids, "created_at": now(), "backend": self.config["backend"]}
                plan["resources"] = [{"resource": self.config["active_link"], "owner": "deployment", "action": "replace_symlink", "before": str(source), "after": str(release)}, {"resource": "database", "owner": "deployment", "action": "migrate", "before": digest(graph["applied"]), "after": digest(graph["targets"])}, {"resource": "maintenance", "owner": "deployment", "action": "gate_and_drain"}]
                if self.config["backend"] == "compose":
                    plan["resources"].append({"resource": "compose_image", "owner": "deployment", "action": "replace", "before": self.backend.image(source), "after": self.backend.image(release)})
                plan["digest"] = digest(plan)
                self.store.put("plans", plan_id, {"plan": plan, "inventory": target, "owned_files": owned, "source_owned_digest": digest(state["owned_files"]), "previous_release": str(source), "previous_inventory": inventory, "previous_owned_files": state["owned_files"]})
                freeze(release)
                return plan
            except Exception:
                if release.exists():
                    # Preparation has not touched live state; retain no unsigned/partial executable release.
                    for path in release.rglob("*"):
                        if not path.is_symlink():
                            os.chmod(path, 0o755 if path.is_dir() else 0o644)
                    os.chmod(release, 0o755)
                    shutil.rmtree(release)
                raise

    def submit(self, plan_id, plan_digest, generation, operation_id, actor_id, request_id):
        operation_id = identifier(operation_id)
        plan_id = identifier(plan_id)
        actor_id = identifier(actor_id)
        request_id = identifier(request_id)
        artifact_id(plan_digest)
        if type(generation) is not int or generation < 0:
            raise AgentError("invalid_generation")
        binding = {"plan_id": plan_id, "plan_digest": plan_digest, "generation": generation, "actor_id": actor_id, "request_id": request_id}
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
            self.ensure_available()
            record = self.store.get("plans", plan_id)
            plan = record["plan"]
            if plan["digest"] != plan_digest or plan["generation"] != generation or self.state()["inventory"]["generation"] != generation:
                raise AgentError("stale_approval", 409)
            if any(row["binding"]["plan_id"] == plan_id for row in self.store.operations()):
                raise AgentError("plan_already_submitted", 409)
            operation = {"id": operation_id, "operation": plan["action"], "action": plan["action"], "state": "queued", "stage": "approved", "generation": generation, "expected_generation": generation, "plan": plan, "plan_digest": plan_digest, "binding": binding, "backup": None, "release": plan["release"], "previous_release": record["previous_release"], "error": None, "created_at": now(), "updated_at": now(), "traffic_released": False, "events": [{"stage": "approved", "at": now()}]}
            self.store.put("operations", operation_id, operation)
        self.worker = threading.Thread(target=self.execute, args=(operation_id,), daemon=False, name=f"deployment-{operation_id}")
        self.worker.start()
        return self.public_operation(operation)

    def public_operation(self, operation, include_plan=True):
        keys = ("id", "operation", "action", "state", "stage", "generation", "expected_generation", "plan_digest", "backup", "release", "error", "created_at", "updated_at", "traffic_released")
        result = {key: operation.get(key) for key in keys}
        if include_plan:
            result["plan"] = operation["plan"]
        return result

    def operation(self, operation_id):
        return self.public_operation(self.store.get("operations", identifier(operation_id)))

    def stage(self, operation, stage, **changes):
        operation.update(changes)
        operation["stage"] = stage
        operation["updated_at"] = now()
        operation["events"].append({"stage": stage, "at": operation["updated_at"]})
        self.store.put("operations", operation["id"], operation)

    def maintain(self, operation):
        atomic_json(self.config["maintenance_marker"], {"operation": operation["id"], "stage": operation["stage"]}, 0o644)

    def execute(self, operation_id):
        with deployment_lock(self.store.root, blocking=True):
            operation = self.store.get("operations", operation_id)
            if operation["state"] != "queued":
                return
            try:
                record = self.store.get("plans", operation["binding"]["plan_id"])
                plan = record["plan"]
                state = self.state()
                if state["inventory"]["generation"] != plan["generation"] or state["release"] != record["previous_release"] or digest(state["owned_files"]) != record["source_owned_digest"]:
                    raise AgentError("stale_execution", 409)
                if str(self.backend.active()) != state["release"]:
                    raise AgentError("active_pointer_conflict", 409)
                if shutil.disk_usage(self.config["releases_root"]).free < self.config.get("minimum_free_bytes", 512 * 1024 * 1024):
                    raise AgentError("insufficient_disk_space", 409)
                check_drift(state["release"], state["owned_files"])
                check_drift(plan["release"], record["owned_files"])
                for artifact in plan["artifacts"]:
                    verified(self.store, self.config, artifact)
                graph = self.backend.probe(plan["release"])
                if graph["migrations"] != plan["migrations"] or graph["applied"] != plan["applied_migrations"] or graph["targets"] != plan["migration_targets"]:
                    raise AgentError("migration_plan_changed", 409)
                self.stage(operation, "maintenance_entering", state="running")
                self.maintain(operation)
                self.backend.drain(state["release"], operation_id)
                self.stage(operation, "drained")
                backup = self.backend.backup(state["release"], operation_id)
                self.stage(operation, "backup_ready", backup=backup)
                # Recheck under the write gate; no DB changes may escape approval between preview and drain.
                graph = self.backend.probe(plan["release"])
                if graph["migrations"] != plan["migrations"] or graph["applied"] != plan["applied_migrations"] or graph["targets"] != plan["migration_targets"]:
                    raise AgentError("migration_plan_changed", 409)
                self.stage(operation, "migration_started")
                self.finish(operation, record)
            except Exception as error:
                code = error.code if isinstance(error, AgentError) else type(error).__name__
                self.stage(operation, "failed", state="failed", error=code)
                # Once the gate exists, no failure clears it or silently rolls files back across a schema change.

    def finish(self, operation, record):
        release = Path(operation["release"])
        self.backend.manage(release, ["migrate", "--noinput"], "migration", code="migration_failed")
        self.stage(operation, "migrated")
        self.backend.manage(release, ["sync_table_catalog"], "migration", code="catalogue_sync_failed")
        self.backend.manage(release, ["check_skeleton_schema"], "runtime", code="runtime_schema_failed")
        self.backend.manage(release, ["validate_provider_bindings", "--database", "default"], "runtime", code="provider_bindings_failed")
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
        self.backend.manage(release, ["sync_deployment_state", "--inventory", release / "inventory.json", "--operation", self.store.path("operations", operation["id"])], "migration", code="state_sync_failed")
        atomic_json(self.store.root / "active.json", {"release": str(release), "inventory": record["inventory"], "owned_files": record["owned_files"]})
        self.stage(operation, "committed", state="succeeded")
        self.backend.manage(release, ["sync_deployment_state", "--inventory", release / "inventory.json", "--operation", self.store.path("operations", operation["id"])], "migration", code="state_sync_failed")
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
                    headers["X-Skeleton-Deployment-Smoke"] = Path(self.config["smoke_token_file"]).read_text().strip()
                with opener.open(Request(release_url, headers=headers), timeout=5) as response:
                    result = json.loads(response.read(4 * 1024 * 1024))
                if result.get("generation") != inventory["generation"] or result.get("core", {}).get("version") != inventory["core"]["version"] or result.get("core", {}).get("digest") != inventory["core"]["digest"] or result.get("core", {}).get("origin") != inventory["core"]["origin"]:
                    raise AgentError("worker_release_mismatch", 409)
                for url in urls:
                    with opener.open(Request(url, headers=headers), timeout=5) as response:
                        if response.status != 200:
                            raise AgentError("application_smoke_failed", 409)
                return
            except Exception as error:
                last = error.code if isinstance(error, AgentError) else "health_unavailable"
                time.sleep(0.5)
        raise AgentError(last, 409)

    def reconcile_interrupted(self):
        with deployment_lock(self.store.root):
            for operation in self.store.operations():
                if operation["state"] in ("queued", "running") or operation["stage"] in ("committed", "traffic_release_started"):
                    self.stage(operation, "interrupted", state="interrupted", error="agent_interrupted")
                    self.maintain(operation)
            state_path = self.store.root / "active.json"
            if state_path.exists():
                state = load_json(state_path)
                try:
                    if str(self.backend.active()) != state["release"]:
                        raise AgentError("active_pointer_conflict")
                    check_drift(state["release"], state["owned_files"])
                except AgentError:
                    atomic_json(self.config["maintenance_marker"], {"stage": "active_state_conflict"}, 0o644)

    def recover(self, operation_id, action, *, acknowledge_new_writes=False):
        with deployment_lock(self.store.root):
            operation = self.store.get("operations", identifier(operation_id))
            if action not in ("restore", "complete", "abort"):
                raise AgentError("invalid_recovery_action")
            if operation["state"] not in ("failed", "interrupted"):
                raise AgentError("operation_not_recoverable", 409)
            record = self.store.get("plans", operation["binding"]["plan_id"])
            state = self.state()
            allowed_states = {
                record["previous_release"]: record["plan"]["generation"],
                operation["release"]: record["inventory"]["generation"],
            }
            if state["inventory"]["generation"] != allowed_states.get(state["release"]):
                raise AgentError("operation_superseded", 409)
            if any(row["id"] != operation["id"] and row["state"] in ("queued", "running", "interrupted")
                   for row in self.store.operations()):
                raise AgentError("recovery_or_operation_pending", 409)
            self.maintain(operation)
            self.backend.drain(self.backend.active(), operation["id"])
            try:
                if action == "complete":
                    check_drift(operation["release"], record["owned_files"])
                    actual = self.backend.probe(operation["release"])
                    approved = {(row["app_label"], row["name"]): row for row in operation["plan"]["migrations"]}
                    if any(approved.get((row["app_label"], row["name"])) != row for row in actual["migrations"]) or actual["targets"] != operation["plan"]["migration_targets"]:
                        raise AgentError("recovery_migration_plan_changed", 409)
                    if not operation["backup"]:
                        operation["backup"] = self.backend.backup(operation["previous_release"], operation["id"])
                    self.stage(operation, "operator_completion", state="running", error=None)
                    self.finish(operation, record)
                else:
                    writes = operation["traffic_released"]
                    if writes and not acknowledge_new_writes:
                        raise AgentError("new_writes_require_explicit_reconciliation", 409)
                    changed_db = any(event["stage"] in ("migration_started", "migrated", "operator_completion") for event in operation["events"])
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
                    if action == "restore" and load_json(operation["backup"])["database_kind"] != "sqlite":
                        self.backend.manage(previous, ["reinstall_record_infrastructure", "--database", "default"], "migration", code="restored_infrastructure_failed")
                    check_drift(previous, previous_owned)
                    self.backend.manage(previous, ["check_skeleton_schema"], "runtime", code="restored_schema_failed")
                    self.backend.manage(previous, ["validate_provider_bindings", "--database", "default"], "runtime", code="restored_bindings_failed")
                    self.backend.manage(previous, ["check", "--deploy"], "runtime", code="restored_checks_failed")
                    self.backend.cutover(previous)
                    self.backend.start(previous, operation["id"])
                    self.health(previous_inventory)
                    atomic_json(self.store.root / "active.json", {"release": str(previous), "inventory": previous_inventory, "owned_files": previous_owned})
                    self.stage(operation, "restored" if action == "restore" else "aborted", state="restored" if action == "restore" else "cancelled", error=None, generation=previous_inventory["generation"])
                    self.backend.manage(previous, ["sync_deployment_state", "--inventory", previous / "inventory.json", "--operation", self.store.path("operations", operation["id"])], "migration", code="state_sync_failed")
                    Path(self.config["maintenance_marker"]).unlink(missing_ok=True)
                return self.public_operation(operation)
            except Exception as error:
                self.stage(operation, "recovery_failed", state="failed", error=error.code if isinstance(error, AgentError) else type(error).__name__)
                raise

    def catalogue_sources(self):
        sources = self.config.get("catalogue_sources", [])
        if not isinstance(sources, list) or any(not isinstance(source, dict) or not isinstance(source.get("id"), str) for source in sources):
            raise AgentError("invalid_catalogue_sources")
        return sources

    def fetch_bytes(self, location, maximum, *, origin=None):
        parts = urlsplit(location)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.fragment:
            raise AgentError("unapproved_catalogue_url")
        if origin is not None and (parts.scheme, parts.hostname, parts.port) != origin:
            raise AgentError("catalogue_artifact_off_origin")
        try:
            with build_opener(NoRedirect()).open(Request(location, headers={"Accept": "application/json, application/octet-stream"}), timeout=self.config.get("catalogue_timeout", 20)) as response:
                content = response.read(maximum + 1)
                if len(content) > maximum:
                    raise AgentError("catalogue_download_too_large", 413)
                return content
        except AgentError:
            raise
        except Exception:
            raise AgentError("catalogue_unavailable", 409) from None

    def check_updates(self):
        result = {"checked_at": now(), "sources": [], "releases": []}
        inventory = self.state()["inventory"]
        installed = {entry["manifest"]["app_id"]: entry["manifest"]["version"] for entry in inventory["apps"] if entry["state"] != "schema_retained"}
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
                body = validate_catalog(envelope, self.config["approved_publishers"], minimum_generation=previous["generation"], channels=source.get("channels", ["stable"]))
                if previous.get("generation") == body["generation"] and previous.get("digest") and previous["digest"] != digest(envelope):
                    raise AgentError("catalogue_generation_content_changed")
                atomic_json(cache, {"generation": body["generation"], "digest": digest(envelope), "envelope": envelope, "checked_at": result["checked_at"]})
                import platform
                django_version = self.config.get("django_version", "5.2.18")
                for release in body["entries"]:
                    compatible = all(Version(value) in SpecifierSet(release["compatibility"][key]) for key, value in (("skeleton", inventory["core"]["version"]), ("python", platform.python_version()), ("django", django_version)))
                    current = installed.get(release["component_id"])
                    state = "incompatible" if not compatible else "current" if current and Version(release["version"]) <= Version(current) else "available"
                    result["releases"].append({"source_id": source["id"], "component_id": release["component_id"], "version": release["version"], "state": state, "notes": release["notes"], "channel": release["channel"], "digest": release["digest"], "compatibility": release["compatibility"]})
            except Exception as error:
                entry.update(state="unavailable", error_code=error.code if isinstance(error, AgentError) else "catalogue_verification_failed")
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
            body = validate_catalog(saved["envelope"], self.config["approved_publishers"], minimum_generation=saved["generation"], channels=source.get("channels", ["stable"]))
        except PackageError:
            raise AgentError("catalogue_verification_failed", 409) from None
        entries = [entry for entry in body["entries"] if entry["component_id"] == component_id and entry["version"] == version]
        if len(entries) != 1:
            raise AgentError("catalogue_release_not_unique", 409)
        entry = entries[0]
        if source.get("url"):
            parts = urlsplit(source["url"])
            content = self.fetch_bytes(urljoin(source["url"], entry["artifact"]), self.config.get("max_upload_bytes", 128 * 1024 * 1024), origin=(parts.scheme, parts.hostname, parts.port))
        else:
            base = Path(source["path"]).parent.resolve()
            target = (base / entry["artifact"]).resolve()
            if target == base or base not in target.parents or not target.is_file() or target.stat().st_size > self.config.get("max_upload_bytes", 128 * 1024 * 1024):
                raise AgentError("catalogue_artifact_path_rejected")
            content = target.read_bytes()
        if hashlib.sha256(content).hexdigest() != entry["digest"]:
            raise AgentError("catalogue_artifact_hash_conflict", 409)
        result = self.upload(content)
        manifest = result["manifest"]
        if manifest.get("app_id", manifest.get("component_id")) != component_id or manifest["version"] != version or manifest["publisher"] != entry["publisher"]:
            raise AgentError("catalogue_artifact_identity_conflict", 409)
        with ZipFile(self.store.path("uploads", result["digest"])) as archive:
            if archive.read("manifest.sig").decode("ascii") != entry["signature"]:
                raise AgentError("catalogue_artifact_signature_conflict", 409)
        return result
