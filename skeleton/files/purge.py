"""Purge of the bytes of deleted attachments after the retention window (#192).

``files.purge`` runs daily per organization, in its system context. An attachment soft deleted
more than ``SKELETON_FILES_DELETED_RETENTION_DAYS`` ago (default 90) has its object deleted from
the private storage and becomes ``purged``: its row stays as a tombstone, the metadata and the
audit keep the history, and ``restore`` refuses it from then on. Live attachments, and deleted
ones inside the window, are never touched. Each row is locked first, so a concurrent restore
either wins (the row is live again and is skipped) or waits and then finds it purged. The bytes
go after the commit; a failure there leaves the object for the next run, which deletes the bytes
of purged rows again.

This is the only physical removal of attachment content, a nominal exception declared in code
(AGENTS.md).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

PURGE_JOB = "files.purge"
DEFAULT_RETENTION_DAYS = 90


def retention() -> timedelta:
    return timedelta(days=int(getattr(settings, "SKELETON_FILES_DELETED_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)))


def _delete_object(key: str) -> None:
    from .storage import StorageError, delete

    try:
        delete(key)
    except (StorageError, OSError):
        pass  # The next run tries again.


def purge_deleted(*, now: Any = None) -> int:
    """Job handler: purge expired deleted attachments; return how many became purged."""
    from .models import Attachment

    now = now or timezone.now()
    limit = now - retention()
    purged = 0
    candidates = (
        Attachment.objects.with_deleted()
        .filter(deleted_at__isnull=False, deleted_at__lte=limit)
        .exclude(state=Attachment.State.PURGED)
        .values_list("pk", flat=True)
    )
    for attachment_id in list(candidates):
        with transaction.atomic():
            row = Attachment.objects.with_deleted().select_for_update().filter(pk=attachment_id).first()
            if row is None or row.deleted_at is None or row.deleted_at > limit or row.state == Attachment.State.PURGED:
                continue  # Restored or purged meanwhile.
            row.state = Attachment.State.PURGED
            row.save(update_fields={"state"})
            key = row.storage_key
            transaction.on_commit(lambda key=key: _delete_object(key))
            purged += 1
    # Bytes a previous run could not delete.
    for key in (
        Attachment.objects.with_deleted().filter(state=Attachment.State.PURGED).values_list("storage_key", flat=True)
    ):
        transaction.on_commit(lambda key=key: _delete_object(key))
    return purged
