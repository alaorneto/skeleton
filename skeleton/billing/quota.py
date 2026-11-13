"""Quotas (#398): the storage allowance with overage, and plan limits on registered metrics.

**Storage.** The allowance is the sum of ``included_storage_bytes × quantity`` of the active
items. The base plan's price says what happens above it:

- ``block``: an upload above the allowance is refused (``QuotaExceeded``, 409);
- ``bill``: uploads go on up to ``overage_hard_cap_bytes`` (no cap: unlimited), and the renewal
  invoices the excess of the closing period, in blocks of ``overage_unit_bytes`` at
  ``overage_unit_amount``, by the **peak** daily snapshot of that period.

``UsageSnapshot`` keeps one row per organization and UTC day, written by the daily job
``billing.usage_snapshot``, with the bytes per app as a report (by the app label of the table
the attachment belongs to). Snapshots older than ``SKELETON_BILLING_USAGE_RETENTION_DAYS`` are
deleted by the same job.

**Plan limits.** A metric is registered in code with ``register_quota_metric(code, counter)``,
where ``counter(organization_id)`` returns the current amount in the current organization's
context. The core registers ``members`` (active memberships), checked when a membership is
admitted or reactivated. A price's ``limits`` maps a metric to a maximum; the limits of the
active items add up (a seat pack adds to the plan); a metric no active item limits is
unlimited. ``check_quota`` raises ``QuotaLimitExceeded`` (``quota_exceeded``).
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.db.models import Max, Sum

from .engine import LineDraft, live_items, register_line_source

OVERAGE_ORDER = 500
_METRIC = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)?\Z")
_metrics: dict[str, Callable[[Any], int]] = {}


def register_quota_metric(code: str, counter: Callable[[Any], int]) -> None:
    """Declare a metric a price can limit; ``code`` is ``members`` or ``<app_label>.<name>``."""
    if not _METRIC.match(code):
        raise ValueError(f"Invalid quota metric code: {code!r}.")
    if code in _metrics and _metrics[code] is not counter:
        raise ValueError(f"Quota metric {code} is already registered.")
    _metrics[code] = counter


def registered_metrics() -> frozenset[str]:
    return frozenset(_metrics)


def _members(organization_id: Any) -> int:
    from skeleton.tenancy.models import Membership

    return Membership.objects.filter(organization_id=organization_id, is_active=True).count()


def register_core_metrics() -> None:
    register_quota_metric("members", _members)


def plan_limit(subscription: Any, metric: str, *, now: datetime) -> int | None:
    """The limit of ``metric`` in the active items, summed; ``None`` when no item limits it."""
    values = [
        item.price.limits[metric] * item.quantity
        for item in live_items(subscription, now=now)
        if metric in item.price.limits
    ]
    return sum(values) if values else None


def check_quota(metric: str, *, increment: int = 1, now: datetime | None = None) -> None:
    """Refuse ``increment`` more of ``metric`` in the current organization above the plan limit."""
    from skeleton.common.exceptions import QuotaLimitExceeded

    from . import engine

    if metric not in _metrics:
        raise ValueError(f"Unknown quota metric {metric!r}.")
    if not engine.billing_enabled() or increment <= 0:
        return
    subscription = engine.current_subscription()
    if subscription is None:
        return
    limit = plan_limit(subscription, metric, now=now or engine.clock())
    if limit is None:
        return
    current = _metrics[metric](subscription.organization_id)
    if current + increment > limit:
        raise QuotaLimitExceeded(metric, limit)


def storage_terms(items: list[Any]) -> tuple[int, str, int | None, int | None, int | None]:
    """``(allowance, overage policy, unit bytes, unit amount, hard cap)`` of the active items."""
    allowance = sum(item.price.included_storage_bytes * item.quantity for item in items)
    plan = next((item.price for item in items if item.price.product.kind == "base_plan"), None)
    if plan is None or plan.storage_overage != "bill":
        return allowance, "block", None, None, None
    return allowance, "bill", plan.overage_unit_bytes, plan.overage_unit_amount, plan.overage_hard_cap_bytes


def storage_quota(items: list[Any]) -> int | None | object:
    """What ``quota_for`` enforces: the allowance (block) or the hard cap (bill; ``None`` unlimited)."""
    from skeleton.common.entitlements import NO_OPINION

    allowance, policy, _, _, cap = storage_terms(items)
    if policy == "bill":
        return max(cap, allowance) if cap is not None else None
    return allowance or NO_OPINION


def _app_of(model_label: str | None) -> str:
    return (model_label or "").split(".", 1)[0] or "unknown"


def snapshot_usage(*, now: datetime | None = None) -> Any:
    """Write today's usage of the current organization (bytes of live attachments, per app)."""
    from skeleton.files.models import Attachment

    from . import engine
    from .models import UsageSnapshot

    now = now or engine.clock()
    organization_id = engine._organization_id()
    rows = (
        Attachment.objects.exclude(state=Attachment.State.PURGED)
        .values("target_table__model_label")
        .annotate(total=Sum("size"))
    )
    by_app: dict[str, int] = {}
    for row in rows:
        label = _app_of(row["target_table__model_label"])
        by_app[label] = by_app.get(label, 0) + int(row["total"] or 0)
    day = now.date()
    snapshot = UsageSnapshot.objects.filter(organization_id=organization_id, day=day).first()
    if snapshot is None:
        snapshot = UsageSnapshot(organization_id=organization_id, day=day)
    snapshot.bytes_used = sum(by_app.values())
    snapshot.by_app = dict(sorted(by_app.items()))
    snapshot.save()
    return snapshot


def purge_snapshots(*, now: datetime | None = None) -> int:
    from . import engine
    from .models import UsageSnapshot

    days = int(getattr(settings, "SKELETON_BILLING_USAGE_RETENTION_DAYS", 400))
    cutoff = (now or engine.clock()).date() - timedelta(days=days)
    deleted, _ = UsageSnapshot.objects.filter(day__lt=cutoff).delete()
    return deleted


def peak_usage(start: datetime, end: datetime) -> int:
    from .models import UsageSnapshot

    peak = UsageSnapshot.objects.filter(day__gte=start.date(), day__lt=end.date()).aggregate(peak=Max("bytes_used"))
    return int(peak["peak"] or 0)


def overage_lines(
    subscription: Any, period_start: datetime, period_end: datetime, lines: list[LineDraft], now: datetime
) -> list[LineDraft]:
    """The excess storage of the closing period, on its renewal invoice (registered as a line source)."""
    from django.utils.translation import gettext

    items = live_items(subscription, now=period_start)
    allowance, policy, unit_bytes, unit_amount, _ = storage_terms(items)
    if policy != "bill" or not unit_bytes or not unit_amount:
        return []
    excess = peak_usage(period_start, period_end) - allowance
    if excess <= 0:
        return []
    blocks = math.ceil(excess / unit_bytes)
    return [
        LineDraft(
            kind="overage",
            description=gettext("Armazenamento excedente"),
            amount=blocks * unit_amount,
            unit_amount=unit_amount,
            quantity=blocks,
            period_start=period_start,
            period_end=period_end,
        )
    ]


def snapshot_job(**payload: object) -> None:
    from . import engine

    if not engine.billing_enabled():
        return
    snapshot_usage()
    purge_snapshots()


def register_quota() -> None:
    register_core_metrics()
    register_line_source(overage_lines, order=OVERAGE_ORDER)
