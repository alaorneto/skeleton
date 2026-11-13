"""Migrate running instances to a new version of their flow (#413).

The app declares, in code, how the steps of one version map onto the next
(``register_flow_migration``). Who administers flows previews what would happen to each
instance, then applies it: a job per organization takes each running or suspended instance of
the old version, under its lock, and re-points it to the new definition.

For each open step run: mapped to the same key, the run (and its tasks) stays; mapped to
another key, the run is cancelled (its tasks too) and the target step is entered in the new
version, creating its tasks again. An instance with an open step the map does not cover, or
inside a parallel block, is skipped and listed. Every migration is written to the instance
history. Only this service changes ``definition`` and ``flow_version`` of an instance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.utils.translation import gettext

from .models import FlowDefinition, FlowInstance, FlowStepRun

MIGRATE_JOB = "flows.migrate_instances"
MIGRATABLE = ("running", "suspended")
PREVIEW_LIMIT = 500


@dataclass(frozen=True)
class MigrationItem:
    instance_id: UUID
    number: str
    status: str
    moves: tuple[tuple[str, str], ...]
    skipped: str


def _require_admin(user: Any) -> None:
    from . import policies

    if not policies.is_flow_admin(user):
        raise PermissionDenied("Flow administration required.")


def _migration(flow_key: Any, from_version: Any) -> Any:
    from .registry import get_migration

    if not isinstance(flow_key, str) or type(from_version) is not int:
        raise ValidationError({"flow_key": gettext("Informe o fluxo e a versão de origem.")})
    try:
        return get_migration(flow_key, from_version)
    except KeyError:
        raise ValidationError({"flow_key": gettext("Nenhuma migração declarada para essa versão.")}) from None


def plan(instance: FlowInstance, migration: Any) -> tuple[tuple[tuple[str, str], ...], str]:
    """``(moves, reason)``: the moves of the open runs, or why the instance cannot migrate."""
    runs = list(
        FlowStepRun.objects.filter(instance=instance, status__in=("active", "waiting", "failed")).order_by("sequence")
    )
    moves = []
    for run in runs:
        if run.scope_id is not None:
            return (), "inside_block"
        target = migration.steps.get(run.step_key)
        if target is None:
            return (), f"unmapped:{run.step_key}"
        moves.append((run.step_key, target))
    return tuple(moves), ""


def preview(user: Any, *, flow_key: Any, from_version: Any) -> tuple[MigrationItem, ...]:
    _require_admin(user)
    migration = _migration(flow_key, from_version)
    rows = FlowInstance.objects.filter(flow_key=flow_key, flow_version=from_version, status__in=MIGRATABLE)
    items = []
    for instance in rows.order_by("created_at", "sysid")[:PREVIEW_LIMIT]:
        moves, reason = plan(instance, migration)
        items.append(MigrationItem(instance.pk, instance.number, instance.status, moves, reason))
    return tuple(items)


def request_migration(user: Any, *, flow_key: Any, from_version: Any) -> Any:
    """Enqueue the migration of the current organization; returns the job receipt."""
    from skeleton.jobs.services import enqueue
    from skeleton.tenancy.context import current_context

    _require_admin(user)
    migration = _migration(flow_key, from_version)
    return enqueue(
        MIGRATE_JOB,
        organization_id=current_context().organization_id,
        payload={"flow_key": migration.key, "from_version": migration.from_version},
    )


def _apply(instance_id: Any, migration: Any, target: FlowDefinition) -> bool:
    from .engine import Execution, _behaviors, lock_instance

    instance = lock_instance(instance_id)
    if instance is None or instance.flow_version != migration.from_version or instance.status not in MIGRATABLE:
        return False
    moves, reason = plan(instance, migration)
    if reason:
        return False
    old = Execution(instance)
    entering = []
    for run in old.open_runs() + list(FlowStepRun.objects.filter(instance=instance, status="failed")):
        destination = migration.steps[run.step_key]
        if destination == run.step_key:
            continue
        step = old.spec.step(run.step_key)
        behavior = _behaviors.get(step.kind)
        if behavior is not None:
            behavior.cancel(old, run, step)
        old._close(run, "migrated", status="cancelled")
        entering.append(destination)
    old.save()
    instance.definition, instance.flow_version = target, migration.to_version
    instance.save(update_fields={"definition", "flow_version"})
    new = Execution(instance)
    new.log(
        "instance_migrated",
        detail={"from": migration.from_version, "to": migration.to_version, "moves": [list(item) for item in moves]},
    )
    for step_key in entering:
        new.activate(step_key)
    new.finish()
    return True


def migrate_instances(*, flow_key: str, from_version: int) -> None:
    """Job handler (system context of one organization)."""
    from .registry import get_flow, get_migration
    from .services import ensure_definition

    try:
        migration = get_migration(flow_key, from_version)
    except KeyError:
        return  # The declaration was removed from code: nothing to do.
    spec = get_flow(flow_key)
    if spec.version != migration.to_version:
        return
    target = ensure_definition(spec)
    ids = list(
        FlowInstance.objects.filter(flow_key=flow_key, flow_version=from_version, status__in=MIGRATABLE)
        .order_by("sysid")
        .values_list("pk", flat=True)
    )
    for instance_id in ids:
        _apply(instance_id, migration, target)


def register_migration_job() -> None:
    from skeleton.jobs.registry import JobType, register_job_type

    register_job_type(
        JobType(
            MIGRATE_JOB,
            migrate_instances,
            {"flow_key": "str", "from_version": "int"},
            run_as="system",
            owner_app="sk_flows",
        )
    )
