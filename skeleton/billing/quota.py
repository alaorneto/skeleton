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

**Plan limits.** The metrics and the limit belong to the core (``skeleton.common.limits`` and
``skeleton.system.limits``, #695); billing is one source of the limit. A price's ``limits`` maps
a metric to a maximum and the limits of the active items add up (a seat pack adds to the plan),
so without an instance default the limit is that sum, as before. ``register_quota_metric`` keeps
the billing's entry point for apps and refuses a code that is already a usage meter;
``check_quota`` raises ``QuotaLimitExceeded`` (``quota_exceeded``).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from django.conf import settings
from django.db.models import Max, Sum

from skeleton.common.limits import (
    LimitContribution,
    register_limit_metric,
    register_limit_source,
    registered_limit_metrics,
)

from .engine import LineDraft, live_items, period_items, register_line_source

OVERAGE_ORDER = 500
# The plan adds to the core limit after the apps (#696, order 100) and before the platform (#697, 300).
PLAN_SOURCE_ORDER = 200


def register_quota_metric(code: str, counter: Callable[[Any], int]) -> None:
    """Declare a metric a price can limit; ``code`` is ``<app_label>.<name>`` (the core registers its own)."""
    from .usage import is_meter

    if isinstance(code, str) and is_meter(code):
        raise ValueError(f"{code} is already a usage meter; a code is either a metric or a meter.")
    register_limit_metric(code, counter)


def registered_metrics() -> frozenset[str]:
    return registered_limit_metrics()


def plan_limit(subscription: Any, metric: str, *, now: datetime) -> int | None:
    """The limit of ``metric`` in the active items, summed; ``None`` when no item limits it."""
    values = [
        item.price.limits[metric] * item.quantity
        for item in live_items(subscription, now=now)
        if metric in item.price.limits
    ]
    return sum(values) if values else None


def plan_source(organization_id: UUID, metric: str) -> Iterable[LimitContribution]:
    """What the subscription adds to the limit of ``metric``: nothing with billing off or no subscription."""
    from skeleton.tenancy.context import current_context

    from . import engine

    if not engine.billing_enabled() or current_context().organization_id != organization_id:
        return ()
    subscription = engine.current_subscription()
    if subscription is None:
        return ()
    limit = plan_limit(subscription, metric, now=engine.clock())
    return () if limit is None else (LimitContribution("billing", limit),)


def check_quota(metric: str, *, increment: int = 1) -> None:
    """Refuse ``increment`` more of ``metric`` in the current organization above its limit (#695).

    ``ValueError`` for an unknown metric; ``QuotaLimitExceeded`` above the limit.
    """
    from skeleton.system.limits import check_limit
    from skeleton.tenancy.context import current_context

    check_limit(current_context().organization_id, metric, increment)


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


def storage_overage_charge(subscription: Any, period_start: datetime, period_end: datetime) -> tuple[int, int, int]:
    """Return ``(blocks, unit_amount, total_amount)`` of storage overage for ``[period_start, period_end)`` (#882).

    Uses ``period_items`` so packs removed at the end of the closing period, packs added mid-period
    (full allowance, matching usage packs) and the outgoing plan of a scheduled downgrade all count.
    """
    items = period_items(subscription, start=period_start, end=period_end)
    allowance, policy, unit_bytes, unit_amount, _ = storage_terms(items)
    if policy != "bill" or not unit_bytes or not unit_amount:
        return 0, 0, 0
    excess = peak_usage(period_start, period_end) - allowance
    if excess <= 0:
        return 0, unit_amount, 0
    blocks = math.ceil(excess / unit_bytes)
    return blocks, unit_amount, blocks * unit_amount


def overage_lines(
    subscription: Any, period_start: datetime, period_end: datetime, lines: list[LineDraft], now: datetime
) -> list[LineDraft]:
    """The excess storage of the closing period, on its renewal invoice (registered as a line source)."""
    from django.utils.translation import gettext

    blocks, unit_amount, amount = storage_overage_charge(subscription, period_start, period_end)
    if blocks <= 0:
        return []
    return [
        LineDraft(
            kind="overage",
            description=gettext("Armazenamento excedente"),
            amount=amount,
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
    from .usage import purge_usage

    snapshot_usage()
    purge_snapshots()
    purge_usage()  # Usage events and counters of old periods (#523).


def register_quota() -> None:
    register_limit_source("billing", plan_source, order=PLAN_SOURCE_ORDER)
    register_line_source(overage_lines, order=OVERAGE_ORDER)
