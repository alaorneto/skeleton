"""The global job ``appearance.orphans`` (#596): objects under ``platform/branding/`` nobody references.

An upload writes the bytes before its row commits; a rollback (or a crash between the two) leaves
an object no row points to. Every six hours this job deletes the objects of the prefix that are
none of the three current keys of the brand row and are older than the grace period of
``files.orphans`` (``skeleton.files.orphans.grace()``). It never lists or deletes ``org/…``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.utils import timezone

ORPHANS_JOB = "appearance.orphans"


def orphan_keys(*, now: Any = None) -> list[str]:
    from skeleton.files.orphans import grace
    from skeleton.files.storage import list_prefix, private_storage

    from .images import is_image_key
    from .models import InstanceBrand

    now = now or timezone.now()
    keys = [key for key in list_prefix("platform/branding/") if is_image_key(key)]
    if not keys:
        return []
    referenced = {
        key for row in InstanceBrand.objects.values_list("logo_key", "logo_dark_key", "icon_key") for key in row if key
    }
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
    """Job handler (global, system context)."""
    from skeleton.files.storage import delete

    removed = 0
    for key in orphan_keys(now=now):
        delete(key)
        removed += 1
    return removed


def register_appearance_jobs() -> None:
    from skeleton.jobs.periodic import register_periodic
    from skeleton.jobs.registry import JobType, register_job_type

    register_job_type(
        JobType(
            code=ORPHANS_JOB,
            handler=clean_orphans,
            scope="global",
            run_as="system",
            owner_app="sk_appearance",
            max_attempts=3,
        )
    )
    register_periodic(ORPHANS_JOB, ORPHANS_JOB, every=timedelta(hours=6), per_organization=False)
