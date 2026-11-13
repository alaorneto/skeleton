"""CSV import with preview, validation and an error report, by job (#183).

The upload stores the file and enqueues ``transfer.import_preview``, which applies every row
through the gateway's own write path (``create_record``/``update_record``: ``write_fields``,
authorization, sequences) inside a savepoint that is always rolled back, so the preview writes
nothing and leaves no audit. After the person confirms a preview without errors,
``transfer.import_apply`` does the same in one transaction that commits only if every row
succeeds: all or nothing.

A row without ``sysid`` creates a record. A row with ``sysid`` updates it and must carry the
``revision`` the person saw; a different revision is an error of that row, never an overwrite.
Columns other than ``sysid``, ``revision`` and the table's ``write_fields`` (never sequence,
system or access fields) refuse the whole file. Both jobs run as the requester, so a revoked
permission fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, models, transaction
from django.utils import timezone
from django.utils.translation import gettext

PREVIEW_JOB = "transfer.import_preview"
APPLY_JOB = "transfer.import_apply"
DEFAULT_MAX_ROWS = 10_000
CONTROL_COLUMNS = ("sysid", "revision")


def max_rows() -> int:
    return int(getattr(settings, "SKELETON_IMPORT_MAX_ROWS", DEFAULT_MAX_ROWS))


def max_bytes() -> int:
    return int(getattr(settings, "SKELETON_UPLOAD_MAX_BYTES", 10 * 1024 * 1024))


def importable_columns(spec: Any) -> tuple[str, ...]:
    sequence = {name for name, _key in getattr(spec, "sequence_fields", ())}
    return tuple(name for name in spec.write_fields if name not in sequence)


@dataclass
class Outcome:
    rows: int = 0
    errors: list[tuple[int, str, str]] = field(default_factory=list)  # (line, column, message)
    fatal: str = ""


def request_import(*, user: Any, table_key: str, upload: Any) -> Any:
    """Store the uploaded CSV and enqueue its preview in the caller's transaction."""
    import io

    from skeleton.files.storage import StorageError, put_stream
    from skeleton.jobs.services import enqueue
    from skeleton.records.services import record_queryset
    from skeleton.tenancy.context import current_context

    from .exports import exportable_spec, retention, transfer_key
    from .models import TransferJob

    spec = exportable_spec(table_key)
    record_queryset(spec, user=user, action="create")
    size = getattr(upload, "size", None)
    if size is None or size > max_bytes():
        raise ValidationError({"file": gettext("O arquivo excede o limite permitido.")})
    content = upload.read()
    try:
        content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValidationError({"file": gettext("Envie um CSV em UTF-8.")}) from None
    organization_id = current_context().organization_id
    key = transfer_key(organization_id)
    try:
        put_stream(key, io.BytesIO(content), max_bytes=max_bytes())
    except StorageError:
        raise ValidationError({"file": gettext("Não foi possível guardar o arquivo.")}) from None
    transfer = TransferJob(
        organization_id=organization_id,
        requester=user,
        kind=TransferJob.Kind.IMPORT,
        table_key=table_key,
        file_key=key,
        filename=(getattr(upload, "name", "") or "import.csv")[:255],
        expires_at=timezone.now() + retention(),
    )
    transfer.save()
    enqueue(PREVIEW_JOB, organization_id=organization_id, payload={"transfer_id": transfer.pk}, actor=user)
    return transfer


def confirm_import(*, user: Any, transfer: Any) -> Any:
    """Queue the application of a preview without errors (state check under row lock)."""
    from skeleton.jobs.services import enqueue

    from .models import TransferJob

    locked = TransferJob.objects.select_for_update().filter(pk=transfer.pk, requester=user).first()
    if locked is None or locked.kind != TransferJob.Kind.IMPORT:
        raise PermissionDenied("Unknown transfer.")
    if locked.state != TransferJob.State.PREVIEWED or locked.error_count:
        raise ValidationError(gettext("Só uma prévia sem erros pode ser aplicada."))
    locked.state = TransferJob.State.APPLYING
    locked.save(update_fields={"state"})
    enqueue(APPLY_JOB, organization_id=locked.organization_id, payload={"transfer_id": locked.pk}, actor=user)
    return locked


def _value(model: Any, name: str, raw: str) -> Any:
    """CSV text to what the write path expects (booleans as bool, blanks as None when nullable)."""
    model_field = model._meta.get_field(name)
    if isinstance(model_field, models.CharField | models.TextField):
        return raw
    if raw.strip() == "":
        return None
    if isinstance(model_field, models.BooleanField):
        lowered = raw.strip().lower()
        if lowered in {"true", "1", "sim", "yes"}:
            return True
        if lowered in {"false", "0", "não", "nao", "no"}:
            return False
        raise ValidationError({name: gettext("Use true ou false.")})
    return raw.strip()


def _messages(error: ValidationError) -> list[tuple[str, str]]:
    if hasattr(error, "error_dict"):
        return [(name, str(message)) for name, items in error.message_dict.items() for message in items]
    return [("", str(message)) for message in error.messages]


def _apply_rows(*, user: Any, spec: Any, header: list[str], rows: Any, outcome: Outcome) -> None:
    from skeleton.common.exceptions import ConcurrentUpdate
    from skeleton.records.services import create_record, record_queryset, update_record

    model = spec.model
    columns = importable_columns(spec)
    limit = max_rows()
    for line, row in enumerate(rows, start=2):
        outcome.rows += 1
        if outcome.rows > limit:
            outcome.fatal = "too_many_rows"
            return
        if len(row) != len(header):
            outcome.errors.append(
                (
                    line,
                    "",
                    gettext("A linha tem %(found)s colunas; o cabeçalho tem %(expected)s.")
                    % {"found": len(row), "expected": len(header)},
                )
            )
            continue
        values = dict(zip(header, row, strict=True))
        try:
            data = {name: _value(model, name, values[name]) for name in columns if name in values}
            with transaction.atomic():
                sysid = (values.get("sysid") or "").strip()
                if not sysid:
                    create_record(spec, user=user, data=data)
                    continue
                revision = (values.get("revision") or "").strip()
                if not revision.isdigit():
                    raise ValidationError({"revision": gettext("Informe a revisão do registro.")})
                record = record_queryset(spec, user=user, action="update").filter(pk=sysid).first()
                if record is None:
                    raise ValidationError({"sysid": gettext("Registro indisponível.")})
                if record.revision != int(revision):
                    raise ValidationError({"revision": gettext("O registro mudou depois da exportação.")})
                update_record(record, spec, user=user, data=data)
        except ValidationError as error:
            outcome.errors.extend((line, column, message) for column, message in _messages(error))
        except (PermissionDenied, ConcurrentUpdate):
            outcome.errors.append((line, "", gettext("Sem permissão ou registro alterado.")))
        except IntegrityError:
            outcome.errors.append((line, "", gettext("A linha conflita com os dados existentes.")))


def _process(*, user: Any, transfer: Any) -> tuple[Any, Outcome]:
    from skeleton.files.storage import open_object

    from .csvio import CsvFormatError, read_csv
    from .exports import exportable_spec

    outcome = Outcome()
    try:
        spec = exportable_spec(transfer.table_key)
    except PermissionDenied:
        outcome.fatal = "unavailable"
        return None, outcome
    with open_object(transfer.file_key) as stream:
        content = stream.read()
    try:
        header, rows = read_csv(content)
    except CsvFormatError as error:
        outcome.fatal = f"format_{error}"
        return spec, outcome
    unknown = [name for name in header if name not in (*CONTROL_COLUMNS, *importable_columns(spec))]
    if unknown:
        outcome.fatal = "unknown_columns"
        outcome.errors.extend((1, name, gettext("Coluna não permitida.")) for name in unknown)
        return spec, outcome
    try:
        _apply_rows(user=user, spec=spec, header=header, rows=rows, outcome=outcome)
    except PermissionDenied:
        outcome.fatal = "unavailable"
    return spec, outcome


def _store_report(transfer: Any, outcome: Outcome) -> None:
    import io

    from skeleton.files.storage import put_stream

    from .csvio import write_csv
    from .exports import MAX_EXPORT_BYTES, transfer_key

    if not outcome.errors:
        return
    key = transfer_key(transfer.organization_id)
    content = write_csv(("linha", "coluna", "mensagem"), outcome.errors)
    put_stream(key, io.BytesIO(content), max_bytes=MAX_EXPORT_BYTES)
    if transfer.report_key:
        from skeleton.files.storage import delete

        old = transfer.report_key
        transaction.on_commit(lambda: delete(old))
    transfer.report_key = key


def _finish(transfer: Any, outcome: Outcome, *, success_state: str) -> None:
    from .models import TransferJob

    _store_report(transfer, outcome)
    transfer.row_count = min(outcome.rows, max_rows())
    transfer.error_count = len(outcome.errors)
    if outcome.fatal:
        transfer.state, transfer.error_code = TransferJob.State.FAILED, outcome.fatal
    elif outcome.errors and success_state == TransferJob.State.APPLIED:
        transfer.state, transfer.error_code = TransferJob.State.FAILED, "row_errors"
    else:
        transfer.state = success_state
    transfer.save(update_fields={"report_key", "row_count", "error_count", "state", "error_code"})
    from skeleton.jobs.services import enqueue

    from .exports import NOTIFY_JOB

    enqueue(NOTIFY_JOB, organization_id=transfer.organization_id, payload={"transfer_id": transfer.pk})


class _Rollback(Exception):
    pass


def run_preview(*, actor: Any, transfer_id: Any) -> None:
    """Job handler: validate every row in a savepoint that is always rolled back."""
    from .models import TransferJob

    transfer = TransferJob.objects.select_for_update().filter(pk=transfer_id, requester=actor).first()
    if transfer is None or transfer.state != TransferJob.State.QUEUED:
        return
    outcome = Outcome()
    try:
        with transaction.atomic():
            _spec, outcome = _process(user=actor, transfer=transfer)
            raise _Rollback
    except _Rollback:
        pass
    _finish(transfer, outcome, success_state=TransferJob.State.PREVIEWED)


def run_apply(*, actor: Any, transfer_id: Any) -> None:
    """Job handler: apply every row in one savepoint, kept only when no row failed."""
    from .models import TransferJob

    transfer = TransferJob.objects.select_for_update().filter(pk=transfer_id, requester=actor).first()
    if transfer is None or transfer.state != TransferJob.State.APPLYING:
        return
    outcome = Outcome()
    try:
        with transaction.atomic():
            _spec, outcome = _process(user=actor, transfer=transfer)
            if outcome.fatal or outcome.errors:
                raise _Rollback
    except _Rollback:
        pass
    _finish(transfer, outcome, success_state=TransferJob.State.APPLIED)
