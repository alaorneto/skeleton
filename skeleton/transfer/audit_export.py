"""CSV export of the filtered audit log (#186), on top of the transfer pipeline of #182.

Only the organization administrator exports, with the same filters as the audit page
(``skeleton.audit.filters``). The job reads through ``AuditLogService.events`` as the
requester, so the rows and the redaction are exactly what the screen shows, and above
``SKELETON_EXPORT_MAX_ROWS`` it fails without a partial file. The global audit of the platform
has no organization and stays out: every transfer belongs to one organization.
"""

from __future__ import annotations

import json
from typing import Any

from django.core.exceptions import PermissionDenied
from django.utils import timezone

AUDIT_KEY = "sk_audit.auditevent"
COLUMNS = (
    "created_at",
    "model_label",
    "record_sysid",
    "record_revision",
    "operation",
    "actor_sysid",
    "request_id",
    "source",
    "changes",
)


def _require_administrator(user: Any) -> None:
    from skeleton.access.policies import is_organization_admin

    if not is_organization_admin(user):
        raise PermissionDenied("Organization administrator required.")


def request_audit_export(*, user: Any, params: Any) -> Any:
    """Validate the filters now and enqueue the export in the caller's transaction."""
    from skeleton.audit.filters import parse_audit_filters
    from skeleton.jobs.services import enqueue
    from skeleton.tenancy.context import current_context

    from .exports import EXPORT_JOB, retention
    from .models import TransferJob

    _require_administrator(user)
    filters = {name: str(value) for name, value in params.items() if name not in {"page", "page_size"} and value}
    parse_audit_filters(filters)
    transfer = TransferJob(
        organization_id=current_context().organization_id,
        requester=user,
        kind=TransferJob.Kind.EXPORT,
        table_key=AUDIT_KEY,
        filters=filters,
        expires_at=timezone.now() + retention(),
    )
    transfer.save()
    enqueue(EXPORT_JOB, organization_id=transfer.organization_id, payload={"transfer_id": transfer.pk}, actor=user)
    return transfer


def build_audit_csv(*, actor: Any, transfer: Any) -> tuple[bytes, int] | str:
    """The CSV bytes and row count, or an error code."""
    from skeleton.audit.filters import parse_audit_filters
    from skeleton.audit.presentation import audit_data
    from skeleton.service_api.audit import AuditLogService

    from .csvio import write_csv
    from .exports import max_rows

    try:
        _require_administrator(actor)
        filters = parse_audit_filters(transfer.filters)
    except PermissionDenied:
        return "unavailable"
    except Exception:  # noqa: BLE001 - stored filters were validated at request time
        return "unavailable"
    service = AuditLogService()
    first = service.events(actor=actor, **filters, page=1, page_size=100)
    if first.count > max_rows():
        return "too_many_rows"
    rows = []
    page = first
    number = 1
    while page.items:
        for event in page.items:
            data = audit_data(event)
            data["changes"] = json.dumps(data["changes"], ensure_ascii=False, sort_keys=True)
            rows.append([data[name] for name in COLUMNS])
        if number * page.page_size >= page.count:
            break
        number += 1
        page = service.events(actor=actor, **filters, page=number, page_size=100)
    return write_csv(COLUMNS, rows), len(rows)


def can_download(user: Any) -> bool:
    try:
        _require_administrator(user)
    except PermissionDenied:
        return False
    return True
