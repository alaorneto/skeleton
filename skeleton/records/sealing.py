"""Seal a record by an authorized operation and verify it later (#527).

Sealing is a business act (signing, posting): there is no generic endpoint. Apps that seal their
own models call :func:`seal_record` in process; other apps use ``RecordSealService``. The payload
holds the table, the record, its organization, the frozen columns, the fingerprints of the live
attachments when the table's policy freezes them, and ``extra``, plain content the app adds (its
child rows, for example). The core signs nothing and keeps no certificate.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from skeleton.common.exceptions import ConcurrentUpdate, RecordSealed
from skeleton.common.models import SEAL_FIELD_NAMES, SealableMixin, SecuredRecord
from skeleton.common.sealing import digest

__all__ = ["attachment_fingerprints", "require_open", "seal_payload", "seal_record", "verify_seal"]


def _policy(model: type) -> Any:
    from skeleton.system.registry import get_spec

    try:
        return get_spec(model._meta.label_lower).seal
    except KeyError:
        return None


def require_open(spec: Any, record: Any, part: str) -> Any:
    """Refuse changing the ``part`` ("attachments" or "comments") of a sealed record when its policy freezes it.

    The target row is locked (``FOR NO KEY UPDATE``) and re-read, so a concurrent seal either
    waits for this change, and covers it, or comes first and this change is refused (#529).
    Returns the record, re-read when locked.
    """
    policy = getattr(spec, "seal", None)
    if policy is None or getattr(policy, part) != "frozen" or not isinstance(record, SealableMixin):
        return record
    locked = type(record).objects.select_for_update(no_key=True).filter(pk=record.pk).first() or record
    if locked.sealed_at is not None:
        raise RecordSealed(f"The record is sealed: its {part} cannot change.")
    return locked


def attachment_fingerprints(record: Any) -> list[dict[str, Any]]:
    """Live attachments of the record, ordered by sysid: what the seal covers (#529)."""
    from skeleton.files.models import Attachment
    from skeleton.system.models import TableDefinition

    table = TableDefinition.objects.filter(model_label=record._meta.label_lower).first()
    if table is None:
        return []
    rows = (
        Attachment.objects.filter(target_table=table, target_sysid=record.pk)
        .order_by("sysid")
        .values("sysid", "sha256", "size", "filename")
    )
    return [dict(row) for row in rows]


def seal_payload(record: Any, *, extra: Any = None) -> dict[str, Any]:
    """The content the seal digest covers; ``extra`` is the app's plain content (or ``None``)."""
    model = type(record)
    if not isinstance(record, SealableMixin):
        raise TypeError(f"{model._meta.label} is not sealable.")
    fields = {}
    for name in model.seal_fields:
        field = model._meta.get_field(name)
        fields[field.column] = getattr(record, field.attname)
    payload: dict[str, Any] = {
        "table": model._meta.label_lower,
        "sysid": record.pk,
        "organization": getattr(record, "organization_id", None),
        "fields": fields,
        "extra": extra,
    }
    policy = _policy(model)
    if policy is not None and policy.attachments == "frozen":
        payload["attachments"] = attachment_fingerprints(record)
    return payload


@transaction.atomic
def seal_record(record: Any, *, user: Any, expected_revision: int, extra: Any = None) -> Any:
    """Seal ``record`` once, after the ``update`` check for a SecuredRecord, with CAS.

    The row is locked (``select_for_update``) before the payload is read, so a child written
    concurrently is either in the payload or refused by the guard (#528). Other bases are
    authorized by the app before calling. Raises ``RecordSealed`` for a sealed or deleted record
    and ``ConcurrentUpdate`` for a stale revision. Returns the sealed record.
    """
    model = type(record)
    if not isinstance(record, SealableMixin):
        raise TypeError(f"{model._meta.label} is not sealable.")
    if record.deleted_at is not None:
        raise RecordSealed("A deleted record cannot be sealed.")
    if isinstance(record, SecuredRecord):
        from skeleton.access.policies import require_record_action

        require_record_action(record, user=user, action="update")
    locked = model.objects.select_for_update().filter(pk=record.pk).first()
    if locked is None:
        raise PermissionDenied
    if locked.sealed_at is not None:
        raise RecordSealed("The record is already sealed.")
    if locked.revision != expected_revision:
        raise ConcurrentUpdate("The supplied revision does not match the record.")
    locked.apply_seal(actor_id=user.pk, digest=digest(seal_payload(locked, extra=extra)), at=timezone.now())
    locked.save(update_fields=set(SEAL_FIELD_NAMES))
    return locked


def verify_seal(record: Any, *, extra: Any = None) -> bool:
    """Recompute the digest from the stored content and compare it with the seal."""
    if not isinstance(record, SealableMixin) or record.sealed_at is None:
        return False
    fresh = type(record).all_objects.filter(pk=record.pk).first()
    if fresh is None or fresh.sealed_at is None:
        return False
    return digest(seal_payload(fresh, extra=extra)) == fresh.seal_digest
