"""Lifecycle of first-party apps in the deployment state (#67).

A first-party app has no ``.skapp`` and no agent inventory entry: it ships in the core wheel and
is installed by ``SKELETON_BUNDLED_APPS``. ``sync_bundled_components`` mirrors that opt-in into
``ComponentInstallation`` (``kind=bundled``), after every ``migrate`` and on demand with
``manage.py sync_bundled_apps``:

- an opted-in app gets ``state=bundled``; the first time, its availability is born ``selected``
  with no grant, so it is unavailable and inactive in every organization (#21/#22);
- an app listed in ``SKELETON_BUNDLED_RETAINED`` (opted out after its migrations ran) becomes
  ``schema_retained``: its tables stay, validated by the frozen snapshot shipped in the wheel,
  and every organization's activation is turned off;
- opting in again flips the row back to ``bundled`` without touching data: UUIDs, records and
  each organization's configuration are kept, and organizations activate it again themselves.

Nothing here deletes data. ``check_skeleton_schema`` reports an app opted out without being
declared retained (``bundled_divergence``).
"""

from __future__ import annotations

from typing import Any

from django.conf import settings

from skeleton.tenancy.context import system_context

KIND = "bundled"
STATE_BUNDLED = "bundled"
STATE_RETAINED = "schema_retained"


def retained_labels() -> dict[str, str]:
    """``name -> app_label`` of the first-party apps declared retained."""
    from skeleton.conf.bundled import BUNDLED_LABELS

    return {name: BUNDLED_LABELS[name] for name in getattr(settings, "SKELETON_BUNDLED_RETAINED", ())}


def _component(manifest: Any) -> dict[str, Any]:
    return {
        "kind": KIND,
        "app_label": manifest.app_label,
        "package_name": manifest.package_name or "",
        "version": manifest.version,
        "digest": "",
        "publisher": (manifest.canonical or {}).get("publisher", ""),
        "state": STATE_BUNDLED,
        "inventory_reference": "core-wheel",
    }


def sync_bundled_components(*, using: str = "default") -> dict[str, list[str]]:
    from skeleton.tenancy.models import Organization

    from .app_registry import all_apps
    from .models import AppInstallation, ComponentInstallation
    from .releases import _close_new_app

    report: dict[str, list[str]] = {"installed": [], "reinstalled": [], "retained": []}
    with system_context(reason="deployment:bundled-apps", using=using):
        for manifest in all_apps():
            if not manifest.bundled:
                continue
            values = _component(manifest)
            identifier = manifest.app_id or manifest.app_label
            row = ComponentInstallation.all_objects.using(using).filter(component_id=identifier).first()
            if row is None:
                ComponentInstallation(component_id=identifier, **values).save(using=using)
                _close_new_app(manifest.app_label, using=using)
                report["installed"].append(manifest.app_label)
                continue
            changes = {name for name, value in values.items() if getattr(row, name) != value}
            if row.state == STATE_RETAINED:
                report["reinstalled"].append(manifest.app_label)
            for name in changes:
                setattr(row, name, values[name])
            if changes:
                row.save(using=using, update_fields=changes)
        retained = sorted(retained_labels().values())
        for row in ComponentInstallation.all_objects.using(using).filter(kind=KIND, app_label__in=retained):
            if row.state != STATE_RETAINED:
                row.state = STATE_RETAINED
                row.save(using=using, update_fields={"state"})
            report["retained"].append(row.app_label)
        organization_ids = list(Organization.all_objects.using(using).values_list("sysid", flat=True))
    for organization_id in organization_ids:
        if not report["retained"]:
            break
        # One transaction per organization: retention turns activation off, never edits it.
        with system_context(reason="deployment:deactivate", organization_id=organization_id, using=using):
            AppInstallation.all_objects.using(using).filter(
                organization_id=organization_id, app_label__in=report["retained"], enabled=True
            ).update(enabled=False)
    return report


def bundled_divergences(*, using: str = "default") -> list[str]:
    """First-party apps whose recorded state contradicts the opt-in of this process."""
    from .app_registry import all_apps
    from .models import ComponentInstallation

    installed = {manifest.app_label for manifest in all_apps() if manifest.bundled}
    retained = set(retained_labels().values())
    problems = []
    for row in ComponentInstallation.all_objects.using(using).filter(kind=KIND).order_by("app_label"):
        if row.state == STATE_BUNDLED and row.app_label not in installed and row.app_label not in retained:
            problems.append(
                f"bundled_divergence: {row.app_label} was opted out without SKELETON_BUNDLED_RETAINED; "
                "its tables would be unclassified."
            )
        if row.state == STATE_RETAINED and row.app_label in installed:
            problems.append(f"bundled_divergence: {row.app_label} is opted in again; run manage.py sync_bundled_apps.")
    for label in sorted(installed):
        if not ComponentInstallation.all_objects.using(using).filter(kind=KIND, app_label=label).exists():
            problems.append(f"bundled_divergence: {label} is opted in but not recorded; run manage.py migrate.")
    return problems
