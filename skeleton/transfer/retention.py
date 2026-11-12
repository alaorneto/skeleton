"""Retention of transfers (#182): the only path that deletes ``TransferJob`` rows and their bytes.

The periodic job ``transfer.retention`` runs every hour per organization, in that
organization's system context. It deletes the rows whose ``expires_at`` passed
(``SKELETON_TRANSFER_RETENTION_DAYS``, 7 by default, counted from the request) and the
private object of each one. The object goes first: a crash between the two leaves a row
without bytes, which the download already answers with 404, never bytes without a row.
"""

from __future__ import annotations

from typing import Any

from django.utils import timezone

RETENTION_JOB = "transfer.retention"
BATCH = 500


def expired(now: Any = None) -> Any:
    from .models import TransferJob

    return TransferJob.objects.filter(expires_at__lte=now or timezone.now())


def purge_expired_transfers(*, now: Any = None) -> dict[str, int]:
    """Job handler (periodic, system context of one organization)."""
    from skeleton.files import storage

    from .models import TransferJob

    rows = objects = 0
    while True:
        batch = list(expired(now).order_by("pk").values_list("pk", "storage_key")[:BATCH])
        if not batch:
            return {"transfers": rows, "objects": objects}
        for _, key in batch:
            if key:
                storage.delete(key)
                objects += 1
        deleted, _ = TransferJob.objects.filter(pk__in=[pk for pk, _ in batch]).delete()
        rows += deleted
