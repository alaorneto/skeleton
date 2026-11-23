"""Collect the figures of one organization and of the instance (#710).

``refresh_organization`` runs in the system context of one organization (a job, one
transaction): it counts members, external people and service accounts, reads the effective
apps, the storage, the billing summary and the registered app metrics, and writes the global
``OrganizationSnapshot`` only when something changed. ``refresh_metrics`` sums the snapshots
into ``InstanceMetric`` for one UTC day.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger("skeleton.insights")

DEFAULT_RETENTION_DAYS = 800
MIN_RETENTION_DAYS = 400
_collectors: dict[str, Callable[[UUID], dict[str, int]]] = {}


def register_instance_metric(code_prefix: str, collector: Callable[[UUID], dict[str, int]]) -> None:
    """An app adds metrics ``<code_prefix>.<name>`` summed over the organizations (in each one's context)."""
    import re

    if not isinstance(code_prefix, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code_prefix):
        raise ValueError(f"Invalid metric prefix: {code_prefix!r}.")
    existing = _collectors.get(code_prefix)
    if existing is not None and existing is not collector:
        raise ValueError(f"Metrics of {code_prefix} are already registered.")
    _collectors[code_prefix] = collector


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=UTC)
    return start, start + timedelta(days=1)


def collect_organization(organization: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """The figures of the organization of the context (read only)."""
    from skeleton.accounts.models import AccountType
    from skeleton.billing.reporting import day_totals, organization_billing_summary
    from skeleton.files.quota import quota_for, usage_for
    from skeleton.system.applications import application_states
    from skeleton.system.limits import MEMBERS, limit_status
    from skeleton.tenancy.models import ExternalAccess, Membership

    now = now or timezone.now()
    memberships = Membership.objects.filter(is_active=True)
    quota, _ = quota_for(organization.pk)
    today = now.astimezone(UTC).date()
    daily = {}
    for day in (today - timedelta(days=1), today):
        start, end = _day_bounds(day)
        daily[day.isoformat()] = day_totals(start=start, end=end)
    app_metrics: dict[str, int] = {}
    for prefix, collector in sorted(_collectors.items()):
        for name, value in (collector(organization.pk) or {}).items():
            code = name if name.startswith(prefix + ".") else f"{prefix}.{name}"
            app_metrics[code] = int(value)
    return {
        "name": organization.name,
        "slug": organization.slug,
        "is_active": organization.is_active,
        "organization_created_at": organization.created_at,
        "members_active": memberships.filter(user__account_type=AccountType.HUMAN).count(),
        "members_limit": limit_status(MEMBERS).limit,
        "external_people": ExternalAccess.objects.filter(is_active=True).count(),
        "service_accounts": memberships.filter(user__account_type=AccountType.SERVICE).count(),
        "apps_effective": sorted(label for label, state in application_states().items() if state["effective"]),
        "storage_bytes_used": int(usage_for(organization.pk).bytes_used),
        "storage_quota_bytes": quota,
        "storage_location_code": _location_code(organization.pk),
        "bundle_code": _bundle_code(organization.pk),
        "billing": organization_billing_summary(now=now),
        "daily": daily,
        "app_metrics": app_metrics,
    }


def _bundle_code(organization_id: Any) -> str:
    from skeleton.system.bundles import bundle_of

    bundle = bundle_of(organization_id)
    return bundle.code if bundle is not None else ""


def _location_code(organization_id: Any) -> str:
    from skeleton.files.locations import location_for

    location = location_for(organization_id)
    return location.code if location is not None else ""


def refresh_organization(**_: Any) -> str:
    """Job (system context of one organization): rewrite its snapshot only when a value changed."""
    from skeleton.tenancy.context import current_context
    from skeleton.tenancy.models import Organization

    from .models import OrganizationSnapshot

    organization = Organization.all_objects.get(pk=current_context().organization_id)
    values = collect_organization(organization)
    now = timezone.now()
    row = OrganizationSnapshot.all_objects.filter(subject_organization_id=organization.pk).first()
    if row is None:
        OrganizationSnapshot(subject_organization=organization, refreshed_at=now, **values).save()
        return "created"
    changed = {name for name, value in values.items() if getattr(row, name) != value}
    if not changed:
        return "unchanged"
    for name in changed:
        setattr(row, name, values[name])
    row.refreshed_at = now
    row.data_version += 1
    row.save(update_fields={*changed, "refreshed_at", "data_version"})
    return "updated"


def refresh_metrics(day: date) -> int:
    """Sum the snapshots into the metrics of ``day`` (UTC); returns how many rows were written."""
    from .models import InstanceMetric, OrganizationSnapshot

    snapshots = list(OrganizationSnapshot.all_objects.all())
    _, end = _day_bounds(day)
    values: dict[tuple[str, str], int] = {}

    def add(code: str, amount: int, currency: str = "") -> None:
        values[(code, currency)] = values.get((code, currency), 0) + int(amount)

    for row in snapshots:
        if row.organization_created_at >= end:
            continue
        add("organizations.total", 1)
        if row.is_active:
            add("organizations.active", 1)
        if row.organization_created_at.astimezone(UTC).date() == day:
            add("organizations.created", 1)
        add("members.total", row.members_active)
        add("storage.bytes", row.storage_bytes_used)
        for label in row.apps_effective:
            add(f"apps.{label}.organizations", 1)
        for code, amount in (row.app_metrics or {}).items():
            add(code, amount)
        billing = row.billing or {}
        if billing:
            currency = billing.get("currency", "")
            add("billing.mrr", billing.get("mrr", 0), currency)
            add("billing.open", billing.get("open_amount", 0), currency)
            add("billing.overdue", billing.get("overdue_amount", 0), currency)
            if billing.get("subscription_status"):
                add(f"billing.subscriptions.{billing['subscription_status']}", 1)
            if billing.get("access_state"):
                add(f"billing.access.{billing['access_state']}", 1)
        for code, by_currency in ((row.daily or {}).get(day.isoformat()) or {}).items():
            for currency, amount in by_currency.items():
                add(code, amount, currency)
    existing = {(row.code, row.currency): row for row in InstanceMetric.all_objects.filter(day=day)}
    written = 0
    for (code, currency), value in sorted(values.items()):
        row = existing.pop((code, currency), None)
        if row is None:
            InstanceMetric(day=day, code=code, currency=currency, value=value).save()
            written += 1
        elif row.value != value:
            row.value = value
            row.save(update_fields={"value"})
            written += 1
    for row in existing.values():
        row.value = 0
        row.save(update_fields={"value"})
    return written


def retention_days() -> int:
    return max(int(getattr(settings, "SKELETON_INSIGHTS_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)), MIN_RETENTION_DAYS)


def daily(**_: Any) -> dict[str, int]:
    """Global job at 00:20 UTC: close the previous day, rewrite today and apply the retention."""
    from .models import InstanceMetric

    today = timezone.now().astimezone(UTC).date()
    written = refresh_metrics(today - timedelta(days=1)) + refresh_metrics(today)
    cutoff = today - timedelta(days=retention_days())
    deleted, _ = InstanceMetric.all_objects.filter(day__lt=cutoff).delete()
    return {"written": written, "deleted": deleted}


def hourly_metrics(**_: Any) -> int:
    """Global job: today's metrics from the latest snapshots."""
    return refresh_metrics(timezone.now().astimezone(UTC).date())
