"""CSV export of authorized records by job (#182).

The request validates the list filters and records a ``TransferJob`` in the caller's
transaction, with a ``transfer.export`` job whose payload is only the transfer id. The job
runs as the requester (``run_as="actor"``), so a revoked membership never reaches the
handler, and re-reads the records through ``record_queryset``: the file holds what the
person could read when it was generated, never more. Download re-authorizes every time.
"""

from __future__ import annotations

import tempfile
from datetime import timedelta
from typing import Any
from uuid import UUID

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.utils import timezone

from skeleton.tenancy.context import current_context

EXPORT_JOB = "transfer.export"
NOTIFICATION = "transfer.finished"
DEFAULT_MAX_ROWS = 100_000
DEFAULT_RETENTION_DAYS = 7
BATCH = 2000
#: Bytes of one export; the row limit is the real bound, this only stops a runaway file.
MAX_BYTES = 2 * 1024 * 1024 * 1024
LIST_PARAMETERS = frozenset({"page", "page_size"})


def max_rows() -> int:
    return int(getattr(settings, "SKELETON_EXPORT_MAX_ROWS", DEFAULT_MAX_ROWS))


def retention() -> timedelta:
    return timedelta(days=int(getattr(settings, "SKELETON_TRANSFER_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)))


def export_fields(spec: Any) -> tuple[str, ...]:
    """Readable, non-sensitive fields: the columns after ``sysid`` and ``revision``."""
    sensitive = set(spec.sensitive_fields)
    return tuple(name for name in spec.read_fields if name not in sensitive)


def header(spec: Any) -> tuple[str, ...]:
    return ("sysid", "revision", *export_fields(spec))


def clean_filters(spec: Any, filters: Any) -> dict[str, str]:
    """The list filters, validated exactly as the list validates them (pagination excluded)."""
    from skeleton.web.services import filter_records

    if filters is None:
        filters = {}
    if not isinstance(filters, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in filters.items()
    ):
        raise ValidationError({"filters": "Use um objeto de filtros textuais."})
    if set(filters) & LIST_PARAMETERS:
        raise ValidationError({"filters": "A exportação não pagina."})
    if set(filters) & set(spec.sensitive_fields):
        # A sensitive column never leaves in the file, so it cannot select rows either.
        raise ValidationError({"filters": "Filtro por campo sensível não permitido."})
    values = {key: value for key, value in filters.items() if value != ""}
    # Validation only: the query is lazy and never runs here.
    filter_records(spec.model._default_manager.none(), spec, values)
    return values


def request_export(spec: Any, *, user: Any, filters: Any = None, idempotency_key: str | None = None) -> Any:
    """Record the export and its job in the caller's transaction; returns the ``TransferJob``."""
    from skeleton.jobs.services import enqueue
    from skeleton.web.services import record_queryset

    from .models import TransferJob

    record_queryset(spec, user=user, action="read")
    values = clean_filters(spec, filters)
    organization_id = current_context().organization_id
    transfer = TransferJob(
        organization_id=organization_id,
        kind=TransferJob.Kind.EXPORT,
        requested_by=user.pk,
        table_key=spec.model._meta.label_lower,
        filters=values,
        expires_at=timezone.now() + retention(),
    )
    if idempotency_key is not None:
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 200:
            raise ValidationError({"idempotency_key": "Chave de 1 a 200 caracteres."})
        existing = TransferJob.objects.filter(requested_by=user.pk, idempotency_key=idempotency_key).first()
        if existing is not None:
            if existing.table_key != transfer.table_key or existing.filters != values:
                raise ValidationError({"idempotency_key": "Chave já usada em outro pedido."})
            return existing
        transfer.idempotency_key = idempotency_key
    transfer.save()
    enqueue(EXPORT_JOB, organization_id=organization_id, payload={"transfer_id": str(transfer.pk)}, actor=user)
    return transfer


def _storage_key(transfer: Any) -> str:
    return f"org/{UUID(str(transfer.organization_id))}/transfers/{UUID(str(transfer.pk))}"


def _filename(spec: Any, transfer: Any) -> str:
    stamp = timezone.localtime(transfer.created_at).strftime("%Y%m%d-%H%M%S")
    return f"{spec.model._meta.label_lower.replace('.', '-')}-{stamp}.csv"


def _finish(transfer: Any, state: str, **values: Any) -> None:
    from .models import TransferJob

    values.update(state=state, finished_at=timezone.now(), updated_at=timezone.now())
    TransferJob.objects.filter(pk=transfer.pk).update(**values)
    _notify(transfer, state)


def _notify(transfer: Any, state: str) -> None:
    from skeleton.notifications.services import Recipients, notify

    notify(
        NOTIFICATION,
        recipients=Recipients(users=(transfer.requested_by,)),
        payload={"transfer": str(transfer.pk), "state": state},
        dedupe_key=f"transfer:{transfer.pk}",
    )


def _rows(spec: Any, queryset: Any, user: Any):
    from skeleton.web.services import records_data

    fields = export_fields(spec)
    names = header(spec)
    batch: list[Any] = []
    for record in queryset.iterator(chunk_size=BATCH):
        batch.append(record)
        if len(batch) == BATCH:
            yield from ([row[name] for name in names] for row in records_data(batch, fields, user=user))
            batch = []
    if batch:
        yield from ([row[name] for name in names] for row in records_data(batch, fields, user=user))


def run_export(*, actor: Any, transfer_id: str) -> None:
    """Job handler (``run_as="actor"``): write the CSV or record why it was not written."""
    from skeleton.files import storage
    from skeleton.web.services import exposed_spec, filter_records, record_queryset

    from . import csv_format
    from .models import TransferJob

    transfer = TransferJob.objects.filter(pk=transfer_id, kind=TransferJob.Kind.EXPORT).first()
    if transfer is None or transfer.requested_by != actor.pk or transfer.state != TransferJob.State.QUEUED:
        return
    TransferJob.objects.filter(pk=transfer.pk).update(state=TransferJob.State.RUNNING, updated_at=timezone.now())
    try:
        spec = exposed_spec(transfer.table_key)
        queryset = filter_records(record_queryset(spec, user=actor, action="read"), spec, transfer.filters)
    except (Http404, PermissionDenied, ValidationError):
        _finish(transfer, TransferJob.State.FAILED, error_code="unauthorized")
        return
    queryset = queryset.order_by("created_at", "sysid")
    total = queryset.count()
    if total > max_rows():
        _finish(transfer, TransferJob.State.FAILED, error_code="too_many_rows", row_count=total)
        return
    key = _storage_key(transfer)
    with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as buffer:
        count = 0
        rows = _rows(spec, queryset, actor)

        def counted():
            nonlocal count
            for row in rows:
                count += 1
                yield row

        for chunk in csv_format.encode_rows(header(spec), counted()):
            buffer.write(chunk)
        buffer.seek(0)
        size = storage.put_stream(key, buffer, max_bytes=MAX_BYTES)
    try:
        _finish(
            transfer,
            TransferJob.State.SUCCEEDED,
            row_count=count,
            size=size,
            storage_key=key,
            filename=_filename(spec, transfer),
        )
    except Exception:
        # The bytes are outside the transaction: never leave them behind a rolled back row.
        storage.delete(key)
        raise


def downloadable(transfer_id: Any, *, user: Any) -> Any:
    """The requester's finished, unexpired export they may still read; anything else is 404."""
    from skeleton.web.services import exposed_spec, record_queryset

    from .models import TransferJob

    try:
        identifier = UUID(str(transfer_id))
    except ValueError:
        raise Http404 from None
    transfer = TransferJob.objects.filter(
        pk=identifier,
        requested_by=user.pk,
        state=TransferJob.State.SUCCEEDED,
        expires_at__gt=timezone.now(),
    ).first()
    if transfer is None or not transfer.storage_key:
        raise Http404
    try:
        # Revoked read access (TablePermission, disabled app) between request and download.
        record_queryset(exposed_spec(transfer.table_key), user=user, action="read")
    except PermissionDenied:
        raise Http404 from None
    return transfer


def transfers_of(user: Any) -> Any:
    from .models import TransferJob

    return TransferJob.objects.filter(requested_by=user.pk).order_by("-created_at", "sysid")


def transfer_url(user: Any, row: Any) -> str | None:
    """Where the notification leads: the transfers page, only for the requester."""
    from django.urls import NoReverseMatch, reverse

    from .models import TransferJob

    transfer_id = row.payload.get("transfer")
    try:
        if not transfer_id or not TransferJob.objects.filter(pk=transfer_id, requested_by=user.pk).exists():
            return None
        return reverse("transfer:transfers", kwargs={"organization_id": row.organization_id})
    except (NoReverseMatch, ValidationError, ValueError):
        return None
