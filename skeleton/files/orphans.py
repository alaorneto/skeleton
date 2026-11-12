"""Cleanup of orphan objects (#62): bytes in the private storage without an attachment row.

An upload writes the bytes before its row commits; a rollback (or a crash between the two)
leaves an object nobody references. ``files.orphans`` runs per organization and deletes the
objects under ``org/<organization>/`` (and its ``branding/``) that are older than the grace
period (1 hour by default) and have no ``Attachment`` row at all nor are the current logo. Rows that are live **or** soft deleted keep
their bytes: restore must keep working, and purging deleted attachments belongs to the LGPD
retention, not to this job.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.conf import settings
from django.utils import timezone

ORPHANS_JOB = "files.orphans"
DEFAULT_GRACE = timedelta(hours=1)


def grace() -> timedelta:
    seconds = getattr(settings, "SKELETON_FILES_ORPHAN_GRACE_SECONDS", None)
    return DEFAULT_GRACE if seconds is None else timedelta(seconds=int(seconds))


def orphan_keys(organization_id: Any, *, now: Any = None) -> list[str]:
    from skeleton.tenancy.models import OrganizationSettings

    from .models import Attachment
    from .storage import list_prefix, private_storage

    now = now or timezone.now()
    keys = list_prefix(f"org/{organization_id}/") + list_prefix(f"org/{organization_id}/branding/")
    if not keys:
        return []
    # Live and soft-deleted rows both keep their bytes (with_deleted keeps the tenant filter).
    referenced = set(
        Attachment.objects.with_deleted().filter(storage_key__in=keys).values_list("storage_key", flat=True)
    )
    # The current logo of the organization (#168); replaced logos are deleted after commit.
    referenced |= set(OrganizationSettings.objects.filter(logo_key__in=keys).values_list("logo_key", flat=True))
    storage = private_storage()
    orphans = []
    for key in keys:
        if key in referenced:
            continue
        try:
            modified = storage.get_modified_time(key)
        except (FileNotFoundError, OSError):
            continue
        if timezone.is_naive(modified):
            modified = timezone.make_aware(modified, timezone.get_default_timezone())
        if now - modified >= grace():
            orphans.append(key)
    return orphans


def clean_orphans(*, now: Any = None) -> int:
    """Job handler (periodic, system context of one organization)."""
    from skeleton.tenancy.context import current_context

    from .storage import delete

    removed = 0
    for key in orphan_keys(current_context().organization_id, now=now):
        delete(key)
        removed += 1
    return removed
