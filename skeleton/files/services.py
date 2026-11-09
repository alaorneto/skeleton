"""Attachments inherit authorization from their target record.

Reading attachments needs read on the target; uploading, removing and
restoring need update on it. A target of another organization, a deleted
target and a table without opt-in all answer 404.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import IO, Any

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404

from skeleton.common.exceptions import ConcurrentUpdate

from .sniffing import HEAD_BYTES, detect
from .storage import StorageError, object_key, put_stream


class UploadTooLarge(ValidationError):
    def __init__(self):
        super().__init__("O arquivo excede o limite permitido.", code="too_large")


class UnsupportedFileType(ValidationError):
    def __init__(self):
        super().__init__("Tipo de arquivo não permitido ou inconsistente com a extensão.", code="unsupported_type")


def policy(spec):
    if spec.attachments is None:
        raise Http404  # Tables without opt-in have no attachment surface at all.
    return spec.attachments


def readable_target(spec, target_sysid, *, user):
    """The live target if the actor can read it; otherwise 404 (missing, foreign or not readable)."""
    from skeleton.access.policies import authorized_queryset

    policy(spec)
    try:
        record = authorized_queryset(spec.model, user=user, action="read").filter(pk=target_sysid).first()
    except PermissionDenied:
        record = None
    if record is None:
        raise Http404
    return record


def writable_target(spec, target_sysid, *, user):
    """Read first (404), then update (403): someone who sees the record learns only that writing is denied."""
    from skeleton.access.policies import require_record_action

    record = readable_target(spec, target_sysid, user=user)
    require_record_action(record, user=user, action="update")
    return record


def _table(spec):
    from skeleton.system.models import TableDefinition

    return TableDefinition.objects.get(model_label=spec.model._meta.label_lower, available=True)


def attachments_for(spec, target_sysid, *, user, include_deleted: bool = False):
    from .models import Attachment

    record = readable_target(spec, target_sysid, user=user)
    queryset = Attachment.objects.with_deleted() if include_deleted else Attachment.objects.all()
    return queryset.filter(target_table=_table(spec), target_sysid=record.pk).order_by("created_at", "sysid")


def _clean_filename(name: str) -> str:
    from pathlib import PurePath

    name = PurePath(str(name).replace("\\", "/")).name.strip()
    if not name or len(name) > 255 or any(ord(char) < 32 for char in name):
        raise ValidationError({"file": "Nome de arquivo inválido."})
    return name


def upload(spec, target_sysid, *, user, stream: IO[bytes], filename: str, size: int | None = None):
    """Store the bytes privately and record the metadata in the current transaction."""
    from .models import Attachment

    rules = policy(spec)
    record = writable_target(spec, target_sysid, user=user)
    limit = min(rules.max_bytes, settings.SKELETON_UPLOAD_MAX_BYTES)
    if size is not None and size > limit:
        raise UploadTooLarge()
    name = _clean_filename(filename)
    head = stream.read(HEAD_BYTES)
    kind = detect(head, name)
    if kind is None or kind not in rules.types:
        raise UnsupportedFileType()
    digest = hashlib.sha256()

    class Hashing:
        def __init__(self):
            self.first = head

        def read(self, amount: int = -1) -> bytes:
            if self.first:
                chunk, self.first = self.first, b""
            else:
                chunk = stream.read(amount)
            digest.update(chunk)
            return chunk

    attachment_id = uuid.uuid4()
    key = object_key(record.organization_id, attachment_id)
    try:
        stored = put_stream(key, Hashing(), max_bytes=limit)
    except StorageError as error:
        if "large" in str(error):
            raise UploadTooLarge() from None
        raise
    scan = getattr(settings, "SKELETON_FILES_SCAN", False)
    attachment = Attachment(
        sysid=attachment_id,
        organization_id=record.organization_id,
        target_table=_table(spec),
        target_sysid=record.pk,
        filename=name,
        content_type=kind,
        size=stored,
        sha256=digest.hexdigest(),
        storage_key=key,
        state=Attachment.State.PENDING if scan else Attachment.State.AVAILABLE,
    )
    try:
        attachment.save()
    except Exception:
        from .storage import delete

        delete(key)  # The row did not persist: never keep its bytes.
        raise
    return attachment


def _locked(spec, target_sysid, attachment_id, *, user, include_deleted: bool):
    from .models import Attachment

    record = writable_target(spec, target_sysid, user=user)
    queryset = Attachment.objects.with_deleted() if include_deleted else Attachment.objects.all()
    attachment = (
        queryset.select_for_update().filter(pk=attachment_id, target_table=_table(spec), target_sysid=record.pk).first()
    )
    if attachment is None:
        raise Http404
    return attachment


def remove(spec, target_sysid, attachment_id, *, user, revision: int) -> Any:
    """Soft delete with CAS; the bytes stay until retention so restore keeps working."""
    attachment = _locked(spec, target_sysid, attachment_id, user=user, include_deleted=False)
    if attachment.revision != revision:
        raise ConcurrentUpdate("Attachment revision is stale.")
    attachment.delete()
    return attachment


def restore(spec, target_sysid, attachment_id, *, user, revision: int) -> Any:
    attachment = _locked(spec, target_sysid, attachment_id, user=user, include_deleted=True)
    if attachment.deleted_at is None:
        raise Http404
    if attachment.revision != revision:
        raise ConcurrentUpdate("Attachment revision is stale.")
    attachment.restore()
    return attachment


def attachment_data(attachment) -> dict[str, Any]:
    """Public metadata; ``storage_key`` never leaves the server."""
    return {
        "sysid": attachment.sysid,
        "revision": attachment.revision,
        "filename": attachment.filename,
        "content_type": attachment.content_type,
        "size": attachment.size,
        "sha256": attachment.sha256,
        "state": attachment.state,
        "created_at": attachment.created_at,
        "created_by": attachment.created_by,
        "deleted_at": attachment.deleted_at,
    }
