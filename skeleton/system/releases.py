"""Platform control plane. Privileged deployment work belongs to the agent."""

import contextlib
import logging
import re
from copy import deepcopy
from datetime import timedelta
from uuid import UUID, uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.tenancy.context import current_context

from .models import ComponentInstallation, DeploymentOperation


class DeploymentUnavailable(RuntimeError):
    pass


def require_platform(user):
    from skeleton.accounts.platform import require_platform_context

    require_platform_context(user)


def require_release_operator(user):
    """Release operations (#34): the platform administrator or the release operator."""
    from skeleton.accounts.platform import UPDATES_OPERATE, require_platform_context

    # The same guard as every platform service (#285), with the release operator's capability.
    require_platform_context(user, capability=UPDATES_OPERATE, message="Release operation required.")


def agent_client():
    from .deployment_client import AgentClient

    socket = getattr(settings, "SKELETON_AGENT_SOCKET", "")
    token = getattr(settings, "SKELETON_AGENT_TOKEN_FILE", "")
    if not socket or not token:
        raise DeploymentUnavailable("Deployment agent is not configured.")
    return AgentClient(socket, token)


PLAN_STATES = {"preparing", "ready", "failed"}


def plan_artifact(upload):
    """Digest to plan and the action to preselect for an upload or fetch result (#495).

    A release package is planned through the core package it carries: the agent kept both.
    """
    if upload.get("kind") == "release":
        return upload["core"]["digest"], "core_update"
    return upload["digest"], None


def update_agent(*, user, digest):
    """Ask the agent to replace itself from an uploaded release package (#497); identifiers only."""
    from skeleton.common.logging import security_event

    require_release_operator(user)
    if not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest):
        raise ValidationError("Invalid release package digest.")
    ctx = current_context()
    result = agent_client().update_agent(digest, actor_id=user.pk)
    security_event(
        "deployment.agent_update_requested",
        request_id=ctx.request_id,
        source="deployment:agent-update",
        outcome=str(result.get("state", ""))[:40],
    )
    return result


UPSTREAM_REPOSITORY = "alaorneto/skeleton"
# Must stay equal to REPOSITORY in deployment_agent/github.py: the core never imports the agent.
GITHUB_REPOSITORY = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9._-]{1,100}\Z")


def update_sources(*, user):
    """The release sources the agent consults (#499); identities only."""
    require_release_operator(user)
    return agent_client().sources()


def save_update_source(*, user, source_id, repository, publisher, channel, private):
    """Register (or replace) a GitHub source; the agent validates it against approved_publishers."""
    require_release_operator(user)
    if not isinstance(repository, str) or not GITHUB_REPOSITORY.fullmatch(repository):
        raise ValidationError("Invalid GitHub repository.")
    return agent_client().set_source(
        id=source_id, github=repository, publisher=publisher, channels=[channel], private=private, actor_id=user.pk
    )


def remove_update_source(*, user, source_id):
    require_release_operator(user)
    return agent_client().remove_source(source_id, actor_id=user.pk)


NOTICE_CACHE_KEY = "skeleton:update-notice:agent"
NOTICE_TTL = 300  # seconds: at most one agent call every 5 minutes per installation.
NOTICE_FAILURE_TTL = 60


def _notice_from_status(status):
    paths = status.get("paths") if isinstance(status, dict) else None
    if not isinstance(paths, list):
        raise ValueError("Invalid agent update status.")
    core, apps = None, 0
    for path in paths:
        if not isinstance(path, dict) or not isinstance(path.get("steps"), list) or not path["steps"]:
            continue
        if path.get("component_id") == "django-skeleton":
            core = path.get("latest") if isinstance(path.get("latest"), str) else None
        else:
            apps += 1
    if core is None and not apps:
        return {"mode": None}
    checked = status.get("checked_at")
    return {"mode": "agent", "core": core, "apps": apps, "checked_at": checked if isinstance(checked, str) else None}


def _notice_key():
    """The notice of the running deployment generation (#739).

    Whoever reconciles an operation may not share this cache (the agent's ``sync_deployment_state``
    runs with the migration profile), so a new release must never depend on an invalidation to
    stop reading the previous release's notice.
    """
    generation = getattr(getattr(settings, "SKELETON_DEPLOYMENT_STATE", None), "generation", 0)
    return f"{NOTICE_CACHE_KEY}:{generation}"


def _running_versions():
    """``{component_id: version}`` of what this process runs: the core and the loaded apps."""
    from skeleton.versioning import core_version

    state = getattr(settings, "SKELETON_DEPLOYMENT_STATE", None)
    running = {
        item["manifest"]["app_id"]: item["manifest"]["version"]
        for item in getattr(state, "apps", ())
        if item.get("state") == "installed"
    }
    core = core_version()["version"]
    if core:
        running["django-skeleton"] = core
    return running


def _same_version(left, right):
    from packaging.version import InvalidVersion, Version

    from skeleton.versioning import normalize_version

    try:
        return Version(normalize_version(left)) == Version(normalize_version(right))
    except (InvalidVersion, TypeError, ValueError):
        return left == right


def _agent_sees_running(status):
    """Whether the agent evaluated the versions this process runs (#739).

    Between a switch of workers and the agent recording the new release (or on a node still
    rolling), the agent answers for the previous release: that answer is not cached as the notice.
    """
    running = _running_versions()
    for path in status["paths"]:
        if not isinstance(path, dict) or path.get("installed") is None:
            continue
        expected = running.get(path.get("component_id"))
        if expected is not None and not _same_version(path["installed"], expected):
            return False
    return True


def update_notice():
    """Summary of pending updates for the platform banner, or None (#501). Never raises."""
    from django.core.cache import cache

    if not getattr(settings, "SKELETON_AGENT_SOCKET", None):
        return _informative_notice(cache)
    key = _notice_key()
    try:
        cached = cache.get(key)
    except Exception:  # noqa: BLE001 - a cache outage means no notice, never a broken page
        return None
    if isinstance(cached, dict):
        return cached if cached.get("mode") else None
    try:
        status = agent_client().update_status(timeout=2)
        notice, ttl = _notice_from_status(status), NOTICE_TTL
        if not _agent_sees_running(status):
            notice, ttl = {"mode": None}, NOTICE_FAILURE_TTL  # The release is still settling.
    except Exception:  # noqa: BLE001 - agent down, slow or answering out of shape: no notice
        notice, ttl = {"mode": None}, NOTICE_FAILURE_TTL
    with contextlib.suppress(Exception):  # A cache outage only drops the hint.
        cache.set(key, notice, ttl)
    return notice if notice.get("mode") else None


def _informative_notice(cache):
    """Without agent (#502): the cached result of the system.update_check job, only informative."""
    from .jobs import GITHUB_NOTICE_KEY

    try:
        cached = cache.get(GITHUB_NOTICE_KEY)
    except Exception:  # noqa: BLE001 - no notice, never a broken page
        return None
    if not isinstance(cached, dict) or cached.get("newer") is not True:
        return None
    return {
        "mode": "informative",
        "core": cached.get("latest"),
        "url": cached.get("url"),
        "checked_at": cached.get("checked_at"),
    }


def invalidate_update_notice():
    from django.core.cache import cache

    with contextlib.suppress(Exception):  # The notice is only a hint.
        cache.delete(_notice_key())


def _plan_id(value):
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError):
        raise DeploymentUnavailable("Invalid agent preview response.") from None


def request_preview(*, user, action, artifacts=(), app_ids=()):
    """Ask the agent to prepare a plan (#298) and return its id; nothing is written to the database.

    Dependency problems still arrive synchronously, as AgentError with details.
    """
    require_release_operator(user)
    if action not in {"install", "update", "uninstall", "reinstall", "core_update"}:
        raise ValidationError("Unsupported deployment operation.")
    summary = agent_client().preview(action, artifacts=list(artifacts), app_ids=list(app_ids))
    if "state" not in summary:
        # An agent from before #298 answers with the whole plan: never guess.
        raise DeploymentUnavailable("Deployment agent is older than this core.")
    if summary["state"] not in PLAN_STATES:
        raise DeploymentUnavailable("Invalid agent preview response.")
    return _plan_id(summary.get("id"))


def preview_status(*, user, plan_id):
    """``{id, state, ...}`` of a plan in the agent; read only."""
    require_release_operator(user)
    status = agent_client().plan(_plan_id(plan_id))
    if status.get("state") not in PLAN_STATES or status.get("id") != str(plan_id):
        raise DeploymentUnavailable("Invalid agent plan response.")
    return status


def record_preview(*, user, plan_id):
    """Mirror a ready plan as a DeploymentOperation in ``preview``; the same plan yields one row."""
    status = preview_status(user=user, plan_id=plan_id)
    if status["state"] != "ready":
        raise DeploymentUnavailable("The deployment plan is not ready.")
    plan = status.get("plan")
    if (
        not isinstance(plan, dict)
        or plan.get("id") != str(plan_id)
        or not isinstance(plan.get("generation"), int)
        or not isinstance(plan.get("digest"), str)
        or len(plan["digest"]) != 64
    ):
        raise DeploymentUnavailable("Invalid agent preview response.")
    action = plan.get("action")
    if action not in {"install", "update", "uninstall", "reinstall", "core_update"}:
        raise DeploymentUnavailable("Invalid agent preview response.")
    existing = DeploymentOperation.objects.filter(plan_digest=plan["digest"]).first()
    if existing is not None:
        return existing
    operation = DeploymentOperation(
        operation=action,
        expected_generation=plan["generation"],
        plan_digest=plan["digest"],
        plan=plan,
    )
    operation.save()
    operation.replaced = _close_open_previews(operation)
    return operation


# A preview that will never run (#821): the operator discarded it, a newer plan for the same
# components replaced it, or the installation changed since it was prepared.
DISCARDED, SUPERSEDED, STALE = "discarded", "superseded", "stale"
CLOSED_PREVIEW_STATES = frozenset({DISCARDED, SUPERSEDED, STALE})


def plan_scope(plan):
    """The components a plan changes: those of the packages it was asked for, and the apps it removes.

    ``plan["components"]`` is the whole target inventory, so only the requested artifacts say
    what the plan is about. A core package counts as the core component.
    """
    if not isinstance(plan, dict):
        return frozenset()

    def items(name):
        value = plan.get(name)
        return value if isinstance(value, list) else ()

    artifacts = {value for value in items("artifacts") if isinstance(value, str)}
    scope = {
        component["component_id"]
        for component in items("components")
        if isinstance(component, dict)
        and isinstance(component.get("component_id"), str)
        and component.get("digest") in artifacts
    }
    if plan.get("action") == "core_update":
        scope.add("django-skeleton")
    scope.update(value for value in items("app_ids") if isinstance(value, str))
    return frozenset(scope)


def _close_preview(row, state):
    row.state = state
    row.stage = state
    row.save(update_fields={"state", "stage"})


def _close_open_previews(operation):
    """Close the open previews a newly recorded one makes useless; returns how many were replaced.

    Same components: replaced (``superseded``). Prepared at an older generation: the
    installation changed since, so the agent would refuse it anyway (``stale``).
    """
    scope = plan_scope(operation.plan)
    replaced = 0
    rows = DeploymentOperation.objects.select_for_update().filter(state="preview").exclude(pk=operation.pk)
    for row in rows.order_by("created_at"):
        if row.expected_generation < operation.expected_generation:
            _close_preview(row, STALE)
        elif scope & plan_scope(row.plan):
            _close_preview(row, SUPERSEDED)
            replaced += 1
    return replaced


def expire_previews(generation):
    """Mark ``stale`` every open preview prepared before ``generation`` (#821)."""
    rows = DeploymentOperation.objects.select_for_update().filter(state="preview", expected_generation__lt=generation)
    for row in rows.order_by("created_at"):
        _close_preview(row, STALE)


def preview_is_stale(row, generation):
    """Whether an open preview can no longer be approved: the installation changed since (#821)."""
    return row.state == "preview" and isinstance(generation, int) and generation != row.expected_generation


def discard_preview(*, user, operation_id, revision):
    """The operator drops a plan awaiting approval (#821); nothing was sent to the agent yet."""
    require_release_operator(user)
    row = DeploymentOperation.objects.select_for_update().get(pk=operation_id)
    if row.revision != revision:
        raise ConcurrentUpdate("Deployment preview changed.")
    if row.state != "preview":
        raise ConcurrentUpdate("Deployment preview has already been submitted or closed.")
    _close_preview(row, DISCARDED)
    return row


def confirm_operation(*, user, operation_id, revision, force_window=False):
    require_release_operator(user)
    operation = DeploymentOperation.objects.select_for_update().get(pk=operation_id)
    if operation.revision != revision:
        raise ConcurrentUpdate("Deployment preview changed.")
    if operation.state != "preview":
        raise ConcurrentUpdate("Deployment preview has already been submitted.")
    inventory = agent_client().inventory()
    if inventory["generation"] != operation.expected_generation:
        raise ConcurrentUpdate("Deployment generation changed; create a new preview.")
    ctx = current_context()
    values = dict(
        plan_id=operation.plan["id"],
        plan_digest=operation.plan_digest,
        generation=operation.expected_generation,
        operation_id=str(operation.pk),
        actor_id=str(user.pk),
        request_id=str(ctx.request_id or uuid4()),
    )
    if force_window:
        values["mode"] = "window"  # A rolling-eligible plan run with the maintenance window anyway (#682).
    operation.state = "approved"
    operation.stage = "awaiting_agent"
    # Keep approval identity until the journal acknowledges delivery. HTTP retries
    # have their own audit IDs, but must resend this exact logical request.
    operation.result = {"submission": values}
    operation.save(update_fields={"state", "stage", "result"})
    _schedule_submission(user=user, values=values)
    return operation


SUBMISSION_STALE_AFTER = timedelta(minutes=2)  # Well above SUBMIT_TIMEOUT (#250).


def submission_can_retry(row, *, now=None):
    """Delivery failed, or it was approved and the delivery callback never reported back (#250)."""
    if row.state == "submission_failed":
        return True
    return (
        row.state == "approved"
        and row.stage == "awaiting_agent"
        and (now or timezone.now()) - row.updated_at >= SUBMISSION_STALE_AFTER
    )


def _schedule_submission(*, user, values):
    values = deepcopy(values)
    request_id = current_context().request_id

    def submit():
        # Values are materialized; never inherit a tenant query after commit.
        try:
            status = agent_client().submit(**values)
            from skeleton.tenancy.context import request_context

            with request_context(actor=user, request_id=request_id, source="deployment:submit"):
                row = DeploymentOperation.objects.select_for_update().get(pk=values["operation_id"])
                reconcile_operation(row, status)
        except Exception as exc:  # noqa: BLE001 - on_commit boundary: any failure must be recorded as submission_failed
            from skeleton.common.logging import security_event
            from skeleton.tenancy.context import request_context

            # Identifiers only (#250): never the plan, the result or the agent token.
            security_event(
                "deployment.submission_failed",
                level=logging.ERROR,
                request_id=request_id,
                source="deployment:submit",
                exc_info=True,
                model_label="sk_system.deploymentoperation",
                target_sysid=values["operation_id"],
                error_class=type(exc).__name__,
                outcome="submission_failed",
            )
            with request_context(actor=user, request_id=request_id, source="deployment:submit"):
                row = DeploymentOperation.objects.select_for_update().get(pk=values["operation_id"])
                if row.state == "approved":
                    row.state = "submission_failed"
                    row.stage = "awaiting_agent"
                    row.error_code = type(exc).__name__[:100]
                    row.save(update_fields={"state", "stage", "error_code"})

    transaction.on_commit(submit)


def retry_submission(*, user, operation_id, revision):
    """Explicit retry of delivery only; the agent UUID remains idempotent."""
    require_release_operator(user)
    row = DeploymentOperation.objects.select_for_update().get(pk=operation_id)
    if row.revision != revision or not submission_can_retry(row):
        raise ConcurrentUpdate("Deployment submission changed.")
    values = row.result.get("submission") if isinstance(row.result, dict) else None
    expected = {
        "plan_id": row.plan["id"],
        "plan_digest": row.plan_digest,
        "generation": row.expected_generation,
        "operation_id": str(row.pk),
    }
    allowed = {*expected, "actor_id", "request_id"}
    extra = set(values) - allowed if isinstance(values, dict) else set()
    if (
        not isinstance(values, dict)
        or not allowed <= set(values)
        or not (not extra or (extra == {"mode"} and values.get("mode") == "window"))
        or any(values.get(name) != value for name, value in expected.items())
    ):
        raise DeploymentUnavailable("Original approval is unavailable; reconcile the agent journal.")
    try:
        UUID(values["actor_id"])
        UUID(values["request_id"])
    except (ValueError, TypeError, AttributeError):
        raise DeploymentUnavailable("Invalid original approval identity.") from None
    # Delivery is not a fresh approval: the agent checks the original generation
    # for a new operation and returns an existing UUID before checking live state.
    row.state = "approved"
    row.stage = "awaiting_agent"
    row.error_code = ""
    row.save(update_fields={"state", "stage", "error_code"})
    _schedule_submission(user=user, values=values)
    return row


def reconcile_operation(row, status):
    try:
        operation_id = UUID(str(status["id"]))
        state = status["state"]
        stage = status["stage"]
        if (
            state not in {"queued", "running", "succeeded", "failed", "interrupted", "restored", "cancelled"}
            or not isinstance(stage, str)
            or not 0 < len(stage) <= 64
            or type(status["generation"]) is not int
            or status["generation"] < 0
            or type(status["expected_generation"]) is not int
        ):
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError):
        raise DeploymentUnavailable("Invalid agent operation response.") from None
    if (
        operation_id != row.pk
        or status["expected_generation"] != row.expected_generation
        or status.get("plan_digest") != row.plan_digest
        or status.get("operation") != row.operation
        or status.get("action") != row.operation
        or "plan" in status
        and status["plan"] != row.plan
    ):
        raise DeploymentUnavailable("Agent operation approval mismatch.")
    if state == "succeeded" and row.state != "succeeded":
        invalidate_update_notice()  # The installed versions changed: recompute the banner (#501).
        expire_previews(status["generation"])  # The plans prepared before can no longer run (#821).
    result = deepcopy(status)
    # Status tickets are short-lived credentials, not durable operation evidence.
    result.pop("status_url", None)
    changes = set()
    values = {
        "state": status["state"],
        "stage": status.get("stage", status["state"]),
        "error_code": str(status.get("error") or "")[:100],
        "result": result,
    }
    for name, value in values.items():
        if getattr(row, name) != value:
            setattr(row, name, value)
            changes.add(name)
    if changes:
        row.save(update_fields=changes)
    return row


def _close_new_app(app_label, *, using="default"):
    """A newly installed app starts ``selected`` with no grant: installing makes nothing available (#22)."""
    from .models import AppAvailability

    if not AppAvailability.all_objects.using(using).filter(app_label=app_label).exists():
        AppAvailability(
            app_label=app_label, mode=AppAvailability.Mode.SELECTED, upgrade_state=AppAvailability.UPGRADE_DONE
        ).save(using=using)


def synchronize_inventory(inventory, *, using="default"):
    generation = inventory["generation"]
    components = []
    core = inventory.get("core")
    if core:
        components.append({**core, "component_id": core.get("component_id", "django-skeleton"), "kind": "core"})
    for entry in inventory.get("apps", []):
        manifest = entry.get("manifest", entry)
        components.append(
            {
                **manifest,
                **{key: entry[key] for key in ("state", "digest", "activation_policy") if key in entry},
                "component_id": manifest.get("app_id", manifest.get("component_id")),
                "kind": "app",
            }
        )
    for data in components:
        identifier = data["component_id"]
        if not identifier:
            raise DeploymentUnavailable("Agent component has no stable identity.")
        row = ComponentInstallation.objects.using(using).filter(component_id=identifier).first()
        values = {
            "kind": data["kind"],
            "app_label": data.get("app_label", ""),
            "package_name": data.get("package_name", ""),
            "version": data["version"],
            "digest": data.get("digest", ""),
            "publisher": data.get("publisher", ""),
            "generation": generation,
            "state": data.get("state", "installed"),
            "inventory_reference": str(data.get("inventory_reference", data.get("digest", "")))[:200],
        }
        if row is None:
            ComponentInstallation(component_id=identifier, **values).save(using=using)
            if data["kind"] == "app" and values["app_label"] and values["state"] == "installed":
                _close_new_app(values["app_label"], using=using)
        else:
            changes = {name for name, value in values.items() if getattr(row, name) != value}
            for name in changes:
                setattr(row, name, values[name])
            if changes:
                row.save(using=using, update_fields=changes)
    return components
