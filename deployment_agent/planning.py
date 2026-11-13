"""Preview of an operation: quick checks, then the release prepared in a thread (#298, #317)."""

import shutil
from pathlib import Path
from uuid import uuid4

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
    artifact_id,
    atomic_json,
    deployment_lock,
    digest,
    file_hash,
    identifier,
    load_json,
    log_internal_error,
    now,
)


def plan_status(service, plan_id):
    """Public view of a plan record: never the inventory, owned files or previous_* (#298)."""
    record = service.store.get("plans", identifier(plan_id))
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


def preview_request(action, artifacts, app_ids):
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


def check_preview(service, action, artifacts, app_ids, *, ignore_plan=None):
    """Checks that need the journal and the active release; the caller holds deployment_lock.

    Dependency problems leave here with their details, synchronously in preview (#298).
    """
    service.ensure_available(ignore_plan=ignore_plan)
    state = service.state()
    source = service.backend.active()
    if str(source) != state["release"]:
        raise AgentError("active_pointer_conflict", 409)
    check_drift(source, state["owned_files"])
    service.backend.preflight(source)  # The operation would fail after maintenance otherwise (#246).
    inventory = state["inventory"]
    old_apps = {entry["manifest"]["app_id"]: entry for entry in inventory["apps"]}
    kinds = []
    manifests = []
    for value in artifacts:
        manifest = verified(service.store, service.config, value).manifest
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
        service.config, service.backend, inventory, manifests, app_ids, suggest=service.catalogue_suggestions
    )
    return state, source, inventory


def prepare(service, plan_id):
    """Build the release and the plan of a ``preparing`` record; ``ready`` or ``failed`` at the end."""
    with deployment_lock(service.store.root, blocking=True):
        try:
            request_record = service.store.get("plans", plan_id)
        except AgentError:
            return None
        if request_record.get("state", "ready") != "preparing":
            return None
        request_record = {key: request_record.get(key) for key in ("request", "generation", "created_at")}
        action = request_record["request"]["action"]
        artifacts, app_ids = request_record["request"]["artifacts"], request_record["request"]["app_ids"]
        try:
            # The lock was released between preview() and here: everything is checked again.
            state, source, inventory = check_preview(service, action, artifacts, app_ids, ignore_plan=plan_id)
            if inventory["generation"] != request_record["generation"]:
                raise AgentError("generation_changed", 409)
            service.backend.manage(source, ["check_skeleton_schema"], "runtime", code="current_schema_invalid")
            required = service.config.get("minimum_free_bytes", 512 * 1024 * 1024)
            if shutil.disk_usage(service.config["releases_root"]).free < required:
                raise AgentError("insufficient_disk_space", 409)
        except Exception as error:  # noqa: BLE001 - any failure marks the plan failed with its code or class
            return preparation_failed(service, plan_id, request_record, error)
        release = Path(service.config["releases_root"]) / f"{inventory['generation'] + 1}-{plan_id}"
        try:
            target = create_release(
                service.config, service.store, service.backend, inventory, artifacts, app_ids, release
            )
            graph = service.backend.probe(release)
            infrastructure = release / "infrastructure.json"
            service.backend.export_infrastructure(release, infrastructure)
            target["frozen_infrastructure"] = {"path": str(infrastructure), "sha256": file_hash(infrastructure)}
            atomic_json(release / "inventory.json", target, 0o644)
            if service.config["backend"] == "compose":
                # Seal the selected image with final static and schema metadata,
                # not merely the provisional payload needed to run preflight.
                service.backend.build_image(release)
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
                "backend": service.config["backend"],
            }
            plan["presentation_changes"] = presentation_diff(inventory, target)
            plan["resources"] = [
                {
                    "resource": service.config["active_link"],
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
            if service.config["backend"] == "compose":
                plan["resources"].append(
                    {
                        "resource": "compose_image",
                        "owner": "deployment",
                        "action": "replace",
                        "before": service.backend.image(source),
                        "after": service.backend.image(release),
                    }
                )
            plan["digest"] = digest(plan)
            service.store.put(
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
            return preparation_failed(service, plan_id, request_record, error)


def preparation_failed(service, plan_id, request_record, error):
    """Record the error code (AgentError) or the class name; the message never leaves the agent."""
    if not isinstance(error, AgentError):
        log_internal_error(error)
    details = getattr(error, "details", None)
    service.store.put(
        "plans",
        plan_id,
        {
            **request_record,
            "state": "failed",
            "error": getattr(error, "code", None) or type(error).__name__,
            "details": details if details else None,
        },
    )


def request_preview(service, action, artifacts, app_ids):
    """The quick checks of a preview and its ``preparing`` record, under the lock; the plan id."""
    artifacts, app_ids = preview_request(action, artifacts, app_ids)
    with deployment_lock(service.store.root):
        state, _, _ = check_preview(service, action, artifacts, app_ids)
        plan_id = str(uuid4())
        service.store.put(
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
    return plan_id
