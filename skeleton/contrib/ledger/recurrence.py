"""Recurring titles (#547): the occurrence dates and the daily job that generates the titles.

A rule falls due on ``start_date`` (monthly rules with ``day_of_month`` on that day of the start
month, or of the next month when it already passed) and then every ``interval`` weeks, months or
years. Monthly days past the end of a shorter month fall on its last day and come back in the next
month (31 Jan, 28 Feb, 31 Mar); a yearly rule from 29 Feb falls on 28 Feb in common years.

The job ``ledger.recurrences`` runs daily per organization in the system context. It creates every
title that falls due up to ``days_ahead`` days ahead, one per occurrence, with the key
``recurrence:<sysid>:<due date>``: a repeated run finds the title and creates nothing. A title that
fails is logged with identifiers only and stops its rule for that run, without undoing the others;
each run creates at most ``RUN_LIMIT`` titles per organization, so a late scheduler does not
generate an unbounded load.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from django.utils import timezone

from .titles import add_months

logger = logging.getLogger("skeleton.contrib.ledger")
JOB = "ledger.recurrences"
RUN_LIMIT = 500
RUN_AT = dt.time(3, 0)


def _anchor(rule: Any) -> int:
    return rule.day_of_month or rule.start_date.day


def first_due(rule: Any) -> dt.date:
    """The first occurrence on or after ``start_date``."""
    start = rule.start_date
    if rule.frequency != "monthly" or not rule.day_of_month:
        return start
    candidate = add_months(start, 0, anchor=rule.day_of_month)
    return candidate if candidate >= start else add_months(start, 1, anchor=rule.day_of_month)


def next_date(rule: Any, current: dt.date) -> dt.date:
    """The occurrence after ``current``; months keep the rule's day, capped at the month's last day."""
    if rule.frequency == "weekly":
        return current + dt.timedelta(weeks=rule.interval)
    months = rule.interval if rule.frequency == "monthly" else 12 * rule.interval
    return add_months(current, months, anchor=_anchor(rule))


def ended(rule: Any, due: dt.date | None) -> bool:
    if due is None:
        return True
    if rule.end_date is not None and due > rule.end_date:
        return True
    return rule.max_occurrences is not None and rule.generated_count >= rule.max_occurrences


def skip_past(rule: Any, today: dt.date) -> dt.date:
    """The first occurrence from ``today`` on, skipping the ones that fell due meanwhile (resume)."""
    due = rule.next_due or first_due(rule)
    while due < today and not ended(rule, due):
        due = next_date(rule, due)
    return due


def title_values(rule: Any, due: dt.date) -> dict[str, Any]:
    return {
        "direction": rule.direction,
        "amount": rule.amount,
        "due_date": due,
        # The competence of the occurrence: an accrual book recognizes it in the month it falls due.
        "issue_date": due,
        "description": rule.description,
        "counterparty_name": rule.counterparty_name,
        "party_ref": rule.party_ref,
        "party_kind": rule.party_kind,
        "source_app": "skeleton_ledger",
        "source_table": "",
        "source_id": None,
        "cost_center_id": rule.cost_center_id,
        "category_account": rule.category_account,
        "idempotency_key": f"recurrence:{rule.pk}:{due.isoformat()}",
        "recurrence": rule,
    }


def generate(rule: Any, *, today: dt.date, budget: int) -> int:
    """Create the due titles of one rule (locked by the caller); returns how many it created."""
    from django.core.exceptions import ValidationError
    from django.db import DatabaseError, transaction

    from skeleton.tenancy.context import current_context

    from . import posting, titles
    from .models import Title

    created = 0
    horizon = today + dt.timedelta(days=rule.days_ahead)
    due = rule.next_due
    while rule.is_active and not ended(rule, due) and due <= horizon and created < budget:
        key = f"recurrence:{rule.pk}:{due.isoformat()}"
        if not Title.objects.filter(source_app="skeleton_ledger", idempotency_key=key).exists():
            try:
                with transaction.atomic(using=current_context().using):
                    titles.create_title(book=rule.book, actor_id=None, values=title_values(rule, due))
            except (ValidationError, DatabaseError, titles.AccountNotBound, posting.PeriodLocked) as error:
                # Identifiers only: never the description or the counterparty name.
                logger.warning(
                    "ledger.recurrence_failed",
                    extra={
                        "event": "ledger.recurrence_failed",
                        "target_sysid": str(rule.pk),
                        "reason_code": type(error).__name__,
                    },
                )
                break
            created += 1
        rule.generated_count += 1
        due = next_date(rule, due)
    # The next candidate stays even after the end: extending the end date or the limit resumes the rule.
    rule.next_due = due
    rule.save(update_fields={"generated_count", "next_due"})
    return created


def generate_due(*, today: dt.date | None = None) -> int:
    """Job handler (system context of the organization): generate the titles that fall due.

    Does nothing while the ledger is off in the organization. Returns how many titles it created.
    """
    from django.core.exceptions import PermissionDenied
    from django.db import transaction
    from django.db.models import F

    from skeleton.system.applications import require_application_enabled
    from skeleton.tenancy.context import current_context

    from .models import RecurringTitle

    try:
        require_application_enabled("skeleton_ledger")
    except PermissionDenied:  # ApplicationUnavailable too: a job of a closed app does nothing.
        return 0
    today = today or timezone.localdate()
    using = current_context().using
    horizon_limit = today + dt.timedelta(days=366)
    candidates = list(
        RecurringTitle.objects.filter(is_active=True, next_due__isnull=False, next_due__lte=horizon_limit)
        .exclude(end_date__lt=F("next_due"))
        .exclude(generated_count__gte=F("max_occurrences"))
        .order_by("next_due", "sysid")
        .values_list("sysid", flat=True)
    )
    created = 0
    for sysid in candidates:
        if created >= RUN_LIMIT:
            break
        with transaction.atomic(using=using):
            rule = (
                RecurringTitle.objects.select_related("book", "category_account")
                .select_for_update(of=("self",))
                .filter(pk=sysid, is_active=True)
                .first()
            )
            if rule is not None:
                created += generate(rule, today=today, budget=RUN_LIMIT - created)
    return created


def register_recurrences() -> None:
    from skeleton.jobs.periodic import register_periodic
    from skeleton.jobs.registry import JobType, register_job_type

    register_job_type(JobType(JOB, generate_due, {}, owner_app="skeleton_ledger", max_attempts=3))
    register_periodic(JOB, JOB, daily_at=RUN_AT, per_organization=True)
