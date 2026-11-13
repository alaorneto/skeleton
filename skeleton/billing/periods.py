"""Billing periods in UTC (#396).

A subscription keeps the day of month it started on (its anchor). A month without that day
ends the period on its last day, and the next period goes back to the anchor: a subscription
started on 31 January renews on 28 (or 29) February, then on 31 March.
"""

from __future__ import annotations

import calendar
from datetime import UTC, datetime

from django.utils import timezone


def require_aware(moment: datetime) -> datetime:
    if timezone.is_naive(moment):
        raise ValueError("Billing dates must be timezone-aware.")
    return moment.astimezone(UTC)


def add_months(moment: datetime, months: int, *, anchor_day: int | None = None) -> datetime:
    """``moment`` plus ``months`` calendar months, on ``anchor_day`` or the month's last day."""
    moment = require_aware(moment)
    index = moment.year * 12 + (moment.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    day = min(anchor_day or moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def period_end(start: datetime, *, interval: str, interval_count: int, anchor_day: int | None = None) -> datetime:
    months = interval_count * (12 if interval == "year" else 1)
    if interval not in ("month", "year") or interval_count < 1:
        raise ValueError("Unknown billing interval.")
    return add_months(start, months, anchor_day=anchor_day)


def seconds_between(start: datetime, end: datetime) -> int:
    return max(0, int((require_aware(end) - require_aware(start)).total_seconds()))
