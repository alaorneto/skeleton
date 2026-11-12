"""Storage quota per organization (#61), respected under concurrent uploads.

Every change of the counter happens in the transaction that writes the attachment, after
``SELECT ... FOR UPDATE`` on the organization row and then on its ``StorageUsage`` row (always
in this order, so writers never deadlock). Two uploads that together exceed the quota
serialize on those locks and the second one fails. The quota is
``StorageQuota.quota_bytes`` when the platform set one for the organization, otherwise
``SKELETON_FILES_ORG_QUOTA_BYTES`` (empty: unlimited). ``files.quota_recount`` rebuilds the
counter from the live attachments every day.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

RECOUNT_JOB = "files.quota_recount"


class QuotaExceeded(Exception):
    """The organization's live attachments would exceed its storage quota."""


@dataclass(frozen=True)
class Usage:
    bytes_used: int
    quota_bytes: int | None  # None: unlimited
    overridden: bool
    recounted_at: Any = None


def quota_for(organization_id: Any) -> tuple[int | None, bool]:
    """``(quota, overridden)``: the platform override or the default setting."""
    from .models import StorageQuota

    row = StorageQuota.objects.filter(quota_organization_id=organization_id).first()
    if row is not None:
        return row.quota_bytes, True
    default = getattr(settings, "SKELETON_FILES_ORG_QUOTA_BYTES", None)
    return (default or None), False


def _locked_usage(organization_id: Any) -> Any:
    from skeleton.tenancy.models import Organization

    from .models import StorageUsage

    # Lock order: organization, then usage. The organization lock also serializes the first
    # creation of the usage row.
    Organization.all_objects.select_for_update(no_key=True).filter(pk=organization_id).values_list(
        "pk", flat=True
    ).first()
    usage = StorageUsage.objects.select_for_update().filter(organization_id=organization_id).first()
    if usage is None:
        usage = StorageUsage(organization_id=organization_id, bytes_used=_live_bytes(organization_id))
        usage.save()
    return usage


def _live_bytes(organization_id: Any) -> int:
    from django.db.models import Sum

    from .models import Attachment

    total = Attachment.objects.filter(organization_id=organization_id).aggregate(total=Sum("size"))["total"]
    return int(total or 0)


@transaction.atomic
def reserve(organization_id: Any, size: int) -> None:
    """Count ``size`` more bytes, or raise ``QuotaExceeded`` without changing anything."""
    usage = _locked_usage(organization_id)
    quota, _ = quota_for(organization_id)
    if quota is not None and usage.bytes_used + size > quota:
        raise QuotaExceeded()
    usage.bytes_used += size
    usage.save(update_fields={"bytes_used"})


@transaction.atomic
def release(organization_id: Any, size: int) -> None:
    usage = _locked_usage(organization_id)
    usage.bytes_used = max(0, usage.bytes_used - size)
    usage.save(update_fields={"bytes_used"})


def usage_for(organization_id: Any) -> Usage:
    """Read-only view for the platform page (no lock)."""
    from .models import StorageUsage

    usage = StorageUsage.objects.filter(organization_id=organization_id).first()
    quota, overridden = quota_for(organization_id)
    if usage is None:
        return Usage(_live_bytes(organization_id), quota, overridden)
    return Usage(usage.bytes_used, quota, overridden, usage.recounted_at)


def recount() -> int:
    """Job handler (periodic, system context of one organization): rebuild the counter."""
    from skeleton.tenancy.context import current_context

    organization_id = current_context().organization_id
    with transaction.atomic():
        usage = _locked_usage(organization_id)
        usage.bytes_used = _live_bytes(organization_id)
        usage.recounted_at = timezone.now()
        usage.save(update_fields={"bytes_used", "recounted_at"})
    return usage.bytes_used


def platform_usage(*, actor: Any, organization: Any) -> Usage:
    """Usage and quota of one organization for the platform page; reads no business data."""
    from skeleton.accounts.platform import require_platform_admin
    from skeleton.tenancy.context import system_context

    require_platform_admin(actor)
    # The same admitted, read-only inspection reason as the platform overview of the organization.
    with system_context(reason="platform:organization-inspect", organization_id=organization.pk):
        return usage_for(organization.pk)


def set_quota(organization: Any, *, quota_bytes: int | None, expected_revision: int | None) -> Any:
    """Platform override (``/platform/``): create, change or remove the organization's quota.

    ``quota_bytes`` ``None`` sets "unlimited"; ``clear_quota`` returns to the default.
    """
    from skeleton.common.exceptions import ConcurrentUpdate

    from .models import StorageQuota

    if quota_bytes is not None and (type(quota_bytes) is not int or quota_bytes < 0):
        raise ValueError("quota_bytes must be a non-negative integer or None.")
    row = StorageQuota.objects.select_for_update().filter(quota_organization_id=organization.pk).first()
    if row is None:
        if expected_revision is not None:
            raise ConcurrentUpdate("The quota was removed by another operation.")
        row = StorageQuota(quota_organization_id=organization.pk, quota_bytes=quota_bytes)
        row.save()
        return row
    if row.revision != expected_revision:
        raise ConcurrentUpdate("The quota was changed by another operation.")
    row.quota_bytes = quota_bytes
    row.save(update_fields={"quota_bytes"})
    return row


def clear_quota(organization: Any, *, expected_revision: int) -> None:
    from skeleton.common.exceptions import ConcurrentUpdate

    from .models import StorageQuota

    row = StorageQuota.objects.select_for_update().filter(quota_organization_id=organization.pk).first()
    if row is None:
        return
    if row.revision != expected_revision:
        raise ConcurrentUpdate("The quota was changed by another operation.")
    row.delete()
