"""Retention and erasure of flow form data (#422).

``flows.purge_form_data`` runs daily per organization, in its system context. An instance closed
(completed or cancelled) longer ago than its flow's ``retention_days`` (0 in the spec uses
``SKELETON_FLOWS_FORM_RETENTION_DAYS``; 0 there keeps the data) has its form data emptied: the
instance's ``data`` and ``sensitive_data`` and the ``values`` and ``sensitive_values`` of each of
its submissions. The skeleton stays: steps, tasks, decisions, dates, outcome and history, and the
instance is marked ``form_purged_at`` so a second run changes nothing. Running, suspended and
failed instances are never touched. The audit never held these values: they are personal fields
(redacted) or sensitive fields of their tables.

When a person is erased (#191), ``forget_person`` removes the fields declared ``personal`` from
the submissions that person sent, and from the instance data while it still holds the value
they sent. Other fields, and other people's submissions, stay.

This is a nominal exception to "no purge" declared in code (AGENTS.md), like ``files.purge``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.utils import timezone

PURGE_JOB = "flows.purge_form_data"
CLOSED = ("completed", "cancelled")


def retention_days(spec: Any) -> int:
    if spec.retention_days:
        return int(spec.retention_days)
    return int(getattr(settings, "SKELETON_FLOWS_FORM_RETENTION_DAYS", 0) or 0)


def expired(spec: Any, finished_at: Any, *, now: Any) -> bool:
    """Whether the form data of an instance closed at ``finished_at`` is past its retention."""
    days = retention_days(spec)
    return days > 0 and finished_at is not None and finished_at <= now - timedelta(days=days)


def purge_form_data(*, now: Any = None) -> int:
    """Job handler: empty the form data of expired closed instances; return how many."""
    from .engine import specification_of
    from .models import FlowDefinition, FlowInstance, FlowSubmission

    now = now or timezone.now()
    purged = 0
    pending = FlowInstance.objects.filter(status__in=CLOSED, finished_at__isnull=False, form_purged_at__isnull=True)
    for definition_id in sorted(set(pending.values_list("definition_id", flat=True))):
        spec = specification_of(FlowDefinition.objects.get(pk=definition_id))
        days = retention_days(spec)
        if days <= 0:
            continue
        limit = now - timedelta(days=days)
        candidates = pending.filter(definition_id=definition_id, finished_at__lte=limit).order_by(
            "finished_at", "sysid"
        )
        for instance_id in list(candidates.values_list("pk", flat=True)[:1000]):
            with transaction.atomic():
                row = FlowInstance.objects.select_for_update().filter(pk=instance_id).first()
                if row is None or row.status not in CLOSED or row.form_purged_at is not None:
                    continue
                if not expired(spec, row.finished_at, now=now):
                    continue
                for submission in FlowSubmission.objects.filter(instance=row).order_by("sysid"):
                    if submission.values or submission.sensitive_values:
                        submission.values, submission.sensitive_values = {}, {}
                        submission.save(update_fields={"values", "sensitive_values"})
                row.data, row.sensitive_data, row.form_purged_at = {}, {}, now
                row.save(update_fields={"data", "sensitive_data", "form_purged_at"})
                purged += 1
    return purged


def forget_person(user_id: UUID) -> int:
    """Erasure (#191) in one organization: the person's ``personal`` form fields; return how many."""
    from .engine import specification_of
    from .models import FlowInstance, FlowSubmission

    changed = 0
    submissions = FlowSubmission.objects.filter(created_by=user_id).select_related("instance__definition")
    for submission_id in list(submissions.order_by("sysid").values_list("pk", flat=True)):
        with transaction.atomic():
            row = FlowSubmission.objects.select_related("instance__definition").filter(pk=submission_id).first()
            if row is None:
                continue
            instance = FlowInstance.objects.select_for_update().get(pk=row.instance_id)
            personal = {
                name for name, item in specification_of(row.instance.definition).data_fields().items() if item.personal
            }
            names = personal & (set(row.values) | set(row.sensitive_values))
            if not names:
                continue
            for column in ("data", "sensitive_data"):
                held = dict(getattr(instance, column))
                source = row.values if column == "data" else row.sensitive_values
                for name in names:
                    if name in held and name in source and held[name] == source[name]:
                        held.pop(name)
                setattr(instance, column, held)
            instance.save(update_fields={"data", "sensitive_data"})
            row.values = {key: value for key, value in row.values.items() if key not in names}
            row.sensitive_values = {key: value for key, value in row.sensitive_values.items() if key not in names}
            row.save(update_fields={"values", "sensitive_values"})
            changed += 1
    return changed


def submissions_of(user_id: UUID, *, limit: int = 5000) -> list[dict[str, Any]]:
    """The holder's export (#190): the forms the person sent in this organization, as sent.

    Sensitive values are the person's own input too, so they are included; values already
    removed by the retention purge or by an erasure are, of course, absent.
    """
    from .models import FlowSubmission

    rows = (
        FlowSubmission.objects.filter(created_by=user_id)
        .select_related("instance")
        .order_by("created_at", "sysid")[:limit]
    )
    return [
        {
            "flow_key": row.instance.flow_key,
            "instance_sysid": row.instance_id,
            "step_key": row.step_key,
            "transition_key": row.transition_key,
            "origin": row.origin,
            "created_at": row.created_at,
            "values": dict(row.values),
            "sensitive_values": dict(row.sensitive_values),
        }
        for row in rows
    ]


def register_retention_job() -> None:
    from datetime import time

    from skeleton.jobs.periodic import register_periodic
    from skeleton.jobs.registry import JobType, register_job_type

    register_job_type(JobType(PURGE_JOB, purge_form_data, {}, run_as="system", max_attempts=3, owner_app="sk_flows"))
    register_periodic(PURGE_JOB, PURGE_JOB, daily_at=time(3, 30))
