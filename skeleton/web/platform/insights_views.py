"""The management figures of the instance (#710) at /platform/insights/: read only."""

from __future__ import annotations

from django.shortcuts import render
from django.views.decorators.http import require_safe

from .guards import platform_page


@platform_page(capability="inventory.read")
@require_safe
def insights(request):
    from skeleton.insights.models import OrganizationSnapshot

    rows = list(OrganizationSnapshot.all_objects.order_by("name"))
    by_currency: dict[str, dict[str, int]] = {}
    apps: dict[str, int] = {}
    for row in rows:
        billing = row.billing or {}
        if billing.get("currency"):
            totals = by_currency.setdefault(billing["currency"], {"mrr": 0, "open": 0, "overdue": 0})
            totals["mrr"] += int(billing.get("mrr") or 0)
            totals["open"] += int(billing.get("open_amount") or 0)
            totals["overdue"] += int(billing.get("overdue_amount") or 0)
        for label in row.apps_effective:
            apps[label] = apps.get(label, 0) + 1
    return render(
        request,
        "skeleton/web/platform/insights.html",
        {
            "platform_section": "insights",
            "total": len(rows),
            "active": sum(1 for row in rows if row.is_active),
            "members": sum(row.members_active for row in rows),
            "by_currency": sorted(by_currency.items()),
            "overdue": [row for row in rows if (row.billing or {}).get("overdue_amount")],
            "apps": sorted(apps.items(), key=lambda item: (-item[1], item[0])),
            "refreshed_at": max((row.refreshed_at for row in rows), default=None),
        },
    )
