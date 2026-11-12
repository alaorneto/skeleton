"""Retention of exports and imports (#182): the only path that deletes their rows and files.

``transfer.retention`` runs daily per organization, in its system context, and removes every
transfer whose ``expires_at`` passed, with its CSV and report objects. The bytes go first; a
missing object is ignored, so a rerun after a crash finishes the job.
"""

from __future__ import annotations

from typing import Any

from django.utils import timezone

RETENTION_JOB = "transfer.retention"


def clean_transfers(*, now: Any = None) -> int:
    from skeleton.files.storage import StorageError, delete

    from .models import TransferJob

    now = now or timezone.now()
    removed = 0
    for transfer in TransferJob.objects.filter(expires_at__lte=now).order_by("expires_at"):
        for key in (transfer.file_key, transfer.report_key):
            if key:
                try:
                    delete(key)
                except StorageError:
                    pass
        TransferJob.objects.filter(pk=transfer.pk).delete()
        removed += 1
    return removed
