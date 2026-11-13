"""CSV export of authorized records by job (#182).

Requesting an export checks, in the request, that the person reads the table and that the
filters are the list's own (``filter_records``); the job ``transfer.export`` then runs as that
person, in their tenant context, so a revocation in between makes it fail closed. The rows are
``authorized_queryset`` plus the same filters, projected by ``records_data``: only the table's
``read_fields`` (never ``sensitive_fields``), and a foreign key the person cannot read comes out
empty, as in the API. Above ``SKELETON_EXPORT_MAX_ROWS`` the job fails without a partial file.
"""

from __future__ import annotations

import io
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone
from django.utils.translation import gettext

EXPORT_JOB = "transfer.export"
NOTIFY_JOB = "transfer.notify"
READY_TYPE = "transfer.ready"
DEFAULT_MAX_ROWS = 100_000
DEFAULT_RETENTION_DAYS = 7
MAX_EXPORT_BYTES = 200 * 1024 * 1024


def max_rows() -> int:
    return int(getattr(settings, "SKELETON_EXPORT_MAX_ROWS", DEFAULT_MAX_ROWS))


def retention() -> timedelta:
    return timedelta(days=int(getattr(settings, "SKELETON_TRANSFER_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)))


def exportable_spec(table_key: str) -> Any:
    """The spec of an exposed business table, or PermissionDenied (unknown looks like denied)."""
    from skeleton.system.registry import get_spec

    try:
        spec = get_spec(table_key)
    except KeyError:
        raise PermissionDenied("Unknown table.") from None
    if not spec.expose_api:
        raise PermissionDenied("Table is not exposed.")
    return spec


def export_fields(spec: Any) -> tuple[str, ...]:
    return tuple(name for name in spec.read_fields if name not in set(spec.sensitive_fields))


def _clean_filters(params: dict[str, Any]) -> dict[str, str]:
    return {str(name): str(value) for name, value in params.items() if name not in {"page", "page_size"}}


def request_export(*, user: Any, table_key: str, params: dict[str, Any]) -> Any:
    """Record the export and enqueue its job in the caller's transaction."""
    from skeleton.jobs.services import enqueue
    from skeleton.records.services import filter_records, record_queryset
    from skeleton.tenancy.context import current_context

    from .models import TransferJob

    spec = exportable_spec(table_key)
    filters = _clean_filters(params)
    # The file never carries sensitive fields, so they cannot narrow it either.
    if set(filters) & set(spec.sensitive_fields):
        raise ValidationError(gettext("Filtro não permitido."))
    filter_records(record_queryset(spec, user=user, action="read"), spec, filters, ignore_empty=True)
    transfer = TransferJob(
        organization_id=current_context().organization_id,
        requester=user,
        kind=TransferJob.Kind.EXPORT,
        table_key=table_key,
        filters=filters,
        expires_at=timezone.now() + retention(),
    )
    transfer.save()
    enqueue(
        EXPORT_JOB,
        organization_id=transfer.organization_id,
        payload={"transfer_id": transfer.pk},
        actor=user,
    )
    return transfer


def transfer_key(organization_id: Any) -> str:
    import uuid

    return f"org/{uuid.UUID(str(organization_id))}/transfers/{uuid.uuid4()}"


def _fail(transfer: Any, code: str) -> None:
    transfer.state = transfer.State.FAILED
    transfer.error_code = code
    transfer.save(update_fields={"state", "error_code"})


def run_export(*, actor: Any, transfer_id: Any) -> None:
    """Job handler (tenant context of the requester)."""
    from skeleton.records.services import filter_records, record_queryset, records_data

    from .csvio import write_csv
    from .models import TransferJob

    transfer = TransferJob.objects.select_for_update().filter(pk=transfer_id, requester=actor).first()
    if transfer is None or transfer.state != TransferJob.State.QUEUED:
        return
    transfer.state = TransferJob.State.RUNNING
    transfer.save(update_fields={"state"})
    from .audit_export import AUDIT_KEY, build_audit_csv

    if transfer.table_key == AUDIT_KEY:
        # The filtered audit log (#186): same rows and redaction as the audit page.
        built = build_audit_csv(actor=actor, transfer=transfer)
        if isinstance(built, str):
            _fail(transfer, built)
            return
        _store(transfer, *built, filename=f"auditoria-{timezone.localdate().isoformat()}.csv")
        return
    try:
        spec = exportable_spec(transfer.table_key)
        rows = filter_records(
            record_queryset(spec, user=actor, action="read"), spec, transfer.filters, ignore_empty=True
        )
    except (PermissionDenied, ValidationError):
        _fail(transfer, "unavailable")
        return
    limit = max_rows()
    rows = rows.order_by("created_at", "sysid")
    if rows.count() > limit:
        _fail(transfer, "too_many_rows")
        return
    fields = export_fields(spec)
    from skeleton.records.services import SYSTEM_FIELDS

    header = tuple(dict.fromkeys((*SYSTEM_FIELDS, *fields)))
    data = []
    chunk = 500
    for start in range(0, limit, chunk):
        page = list(rows[start : start + chunk])
        if not page:
            break
        data.extend(records_data(page, fields, user=actor))
    content = write_csv(header, ([row.get(name) for name in header] for row in data))
    _store(
        transfer, content, len(data), filename=f"{spec.model._meta.model_name}-{timezone.localdate().isoformat()}.csv"
    )


def _store(transfer: Any, content: bytes, rows: int, *, filename: str) -> None:
    from skeleton.files.storage import put_stream

    from .models import TransferJob

    key = transfer_key(transfer.organization_id)
    put_stream(key, io.BytesIO(content), max_bytes=MAX_EXPORT_BYTES)
    transfer.file_key = key
    transfer.filename = filename
    transfer.row_count = rows
    transfer.state = TransferJob.State.READY
    transfer.save(update_fields={"file_key", "filename", "row_count", "state"})
    # A system job tells the requester: the fan-out never notifies the actor of the action itself.
    from skeleton.jobs.services import enqueue

    enqueue(NOTIFY_JOB, organization_id=transfer.organization_id, payload={"transfer_id": transfer.pk})


def notify_ready(*, transfer_id: Any) -> None:
    """Job handler (system context of the organization)."""
    from skeleton.notifications.services import Recipients, notify

    from .models import TransferJob

    finished = (
        TransferJob.State.READY,
        TransferJob.State.PREVIEWED,
        TransferJob.State.APPLIED,
        TransferJob.State.FAILED,
    )
    transfer = TransferJob.objects.filter(pk=transfer_id, state__in=finished).first()
    if transfer is None:
        return
    notify(
        READY_TYPE,
        recipients=Recipients(users=(transfer.requester_id,)),
        payload={"transfer": str(transfer.pk)},
        dedupe_key=f"transfer:{transfer.pk}:{transfer.state}",
    )
