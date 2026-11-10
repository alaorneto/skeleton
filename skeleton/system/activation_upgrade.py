"""One-time upgrade to closed-by-default availability and activation (#22).

Before #22, an app without an availability row was available to every organization,
and an app without ``AppInstallation`` was active wherever its manifest said
``default_enabled``. Closing both defaults would silently take apps away from the
organizations already using them, so this upgrade materializes the old effective
state as rows, then never again infers anything from the manifest.

It is a command, not a data migration: the decision reads the live manifest registry
and deployment inventory, which are not historical migration state, and writing
through historical models would bypass the tenant context. The phases are resumable
through ``AppAvailability.upgrade_state``:

1. one global transaction creates ``mode=all`` (``pending``) for every app installed
   before the upgrade that has no row, and marks pre-#22 rows (``""``) ``pending``;
2. one transaction per organization, under
   ``system_context(reason="upgrade:app-activation")``, creates
   ``AppInstallation(enabled=True)`` for each pending app whose old default was true
   and that the organization never configured;
3. one global transaction marks the pending rows ``done``.

Interrupting between phases and running again finishes the work without duplicating
rows: phase 1 and 2 only create what is missing, and only phase 3 ends the upgrade.
An app installed after the upgrade is born ``selected`` by ``synchronize_inventory``
and never passes through here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from django.db import transaction

REASON = "upgrade:app-activation"


@dataclass
class UpgradeReport:
    created_availability: list[str] = field(default_factory=list)
    marked_pending: list[str] = field(default_factory=list)
    activations: dict[UUID, list[str]] = field(default_factory=dict)
    completed: list[str] = field(default_factory=list)

    @property
    def activation_count(self) -> int:
        return sum(len(labels) for labels in self.activations.values())


def _legacy_labels(registry, *, using):
    """Apps that existed before the upgrade, so their old "available to all" must be kept.

    With an agent, these are the apps the database already knows as installed: an app new
    in the release being deployed is not in ``ComponentInstallation`` yet and is born
    ``selected`` later. Without any inventory (manual or development deployment), every
    loaded app is legacy, but only on the first run: once a row carries an upgrade state,
    an app without a row is a new one.
    """
    from .models import AppAvailability, ComponentInstallation

    components = ComponentInstallation.all_objects.using(using).filter(kind="app", deleted_at__isnull=True)
    if components.exists():
        return set(
            components.filter(state__in=("installed", "project-local")).values_list("app_label", flat=True)
        ) & set(registry)
    started = (
        AppAvailability.all_objects.using(using)
        .filter(upgrade_state__in=(AppAvailability.UPGRADE_PENDING, AppAvailability.UPGRADE_DONE))
        .exists()
    )
    return set() if started else set(registry)


def _prepare(registry, report, *, dry_run, using):
    from skeleton.tenancy.context import system_context

    from .models import AppAvailability

    with transaction.atomic(using=using), system_context(reason=REASON, using=using):
        rows = {row.app_label: row for row in AppAvailability.all_objects.using(using).select_for_update()}
        legacy = _legacy_labels(registry, using=using)
        for label in sorted(registry):
            row = rows.get(label)
            if row is None and label in legacy:
                report.created_availability.append(label)
                if not dry_run:
                    AppAvailability(
                        app_label=label, mode=AppAvailability.Mode.ALL, upgrade_state=AppAvailability.UPGRADE_PENDING
                    ).save(using=using)
            elif row is not None and row.deleted_at is None and row.upgrade_state == "":
                report.marked_pending.append(label)
                if not dry_run:
                    row.upgrade_state = AppAvailability.UPGRADE_PENDING
                    row.save(using=using, update_fields={"upgrade_state"})
        pending = {
            label
            for label, row in rows.items()
            if row.deleted_at is None and row.upgrade_state == AppAvailability.UPGRADE_PENDING
        }
        return sorted((pending | set(report.created_availability) | set(report.marked_pending)) & set(registry))


def _activate(registry, pending, report, *, dry_run, using):
    from skeleton.tenancy.context import system_context
    from skeleton.tenancy.models import Organization

    from .app_registry import validate_configuration
    from .models import AppInstallation

    defaults = [label for label in pending if registry[label].default_enabled]
    if not defaults:
        return
    with system_context(reason=REASON, using=using):
        organizations = list(
            Organization.all_objects.using(using)
            .filter(deleted_at__isnull=True)
            .order_by("sysid")
            .values_list("sysid", flat=True)
        )
    for organization_id in organizations:
        # One independent transaction per organization; never one context over all of them.
        with (
            transaction.atomic(using=using),
            system_context(reason=REASON, organization_id=organization_id, using=using),
        ):
            configured = set(
                AppInstallation.all_objects.using(using)
                .filter(organization_id=organization_id, app_label__in=defaults)
                .values_list("app_label", flat=True)
            )
            missing = [label for label in defaults if label not in configured]
            if missing:
                report.activations[organization_id] = missing
            if dry_run:
                continue
            for label in missing:
                AppInstallation(
                    organization_id=organization_id,
                    app_label=label,
                    enabled=True,
                    configuration=validate_configuration(registry[label], {}),
                ).save(using=using)


def _finish(pending, report, *, dry_run, using):
    from skeleton.tenancy.context import system_context

    from .models import AppAvailability

    report.completed = list(pending)
    if dry_run or not pending:
        return
    with transaction.atomic(using=using), system_context(reason=REASON, using=using):
        for row in AppAvailability.all_objects.using(using).select_for_update().filter(app_label__in=pending):
            if row.upgrade_state == AppAvailability.UPGRADE_PENDING:
                row.upgrade_state = AppAvailability.UPGRADE_DONE
                row.save(using=using, update_fields={"upgrade_state"})


def upgrade_app_activation(*, dry_run=False, using="default", stop_after=None) -> UpgradeReport:
    """Run (or, with ``dry_run``, only describe) the resumable upgrade; ``stop_after`` is for tests."""
    from .app_registry import all_apps

    registry = {manifest.app_label: manifest for manifest in all_apps()}
    report = UpgradeReport()
    pending = _prepare(registry, report, dry_run=dry_run, using=using)
    if stop_after == "prepare":
        return report
    _activate(registry, pending, report, dry_run=dry_run, using=using)
    if stop_after == "activate":
        return report
    _finish(pending, report, dry_run=dry_run, using=using)
    return report


def apps_without_availability(*, using="default") -> list[str]:
    """Loaded apps with no availability row: closed for everyone (check_skeleton_schema warns)."""
    from .app_registry import all_apps
    from .models import AppAvailability

    known = set(AppAvailability.all_objects.using(using).values_list("app_label", flat=True))
    return sorted(manifest.app_label for manifest in all_apps() if manifest.app_label not in known)
