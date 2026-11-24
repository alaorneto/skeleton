"""A person's inbox: list, counter, read/unread/archive and authorized opening.

Reading across organizations goes through self-discovery (``own_rows``) and only
for organizations where the person still has an active membership. Every write
happens inside the tenant of the row; a row of someone else answers 404.
"""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from django.http import Http404
from django.utils import timezone

from skeleton.common.exceptions import MissingTenantContext
from skeleton.tenancy.context import current_context, memberships_for_user, own_rows

COUNTER_CAP = 99


def _active_organizations(user) -> list[UUID]:
    return [row["organization_id"] for row in memberships_for_user(user)]


def inbox_queryset(user, *, include_archived: bool = False):
    """Rows of ``user``: the current tenant when there is one, else every active organization."""
    from .models import NotificationRecipient

    organization_id = current_context().organization_id
    if organization_id is not None:
        rows = NotificationRecipient.objects.filter(user=user)
    else:
        rows = own_rows(NotificationRecipient, user).filter(organization_id__in=_active_organizations(user))
    if not include_archived:
        rows = rows.filter(archived_at__isnull=True)
    # No join with Notification: across organizations only the person's own rows are visible.
    return rows.order_by("-updated_at", "sysid")


def unread_count(user, *, organization_ids: Iterable | None = None) -> int:
    """Unread, unarchived rows of every active organization: a constant number of queries."""
    from .models import NotificationRecipient

    if not getattr(user, "is_authenticated", False):
        return 0
    if organization_ids is None:
        organization_ids = _active_organizations(user)
    return (
        own_rows(NotificationRecipient, user)
        .filter(organization_id__in=list(organization_ids), read_at__isnull=True, archived_at__isnull=True)
        .count()
    )


def counter_label(count: int) -> str:
    return f"{COUNTER_CAP}+" if count > COUNTER_CAP else str(count)


def _own_rows_in_tenant(user, ids: Iterable):
    from .models import NotificationRecipient

    if current_context().organization_id is None:
        raise MissingTenantContext("Inbox changes happen inside the row's organization.")
    try:
        ids = {UUID(str(value)) for value in ids}
    except (ValueError, TypeError):
        raise Http404 from None
    rows = NotificationRecipient.objects.filter(user=user, pk__in=ids)
    if rows.count() != len(ids):
        raise Http404  # Someone else's row and a missing row look the same.
    return rows


def mark_read(user, ids: Iterable, *, read: bool = True) -> int:
    """Idempotent: marking read keeps the first read time; marking unread clears it."""
    rows = _own_rows_in_tenant(user, ids)
    if read:
        return rows.filter(read_at__isnull=True).update(read_at=timezone.now())
    return rows.filter(read_at__isnull=False).update(read_at=None)


def archive(user, ids: Iterable) -> int:
    rows = _own_rows_in_tenant(user, ids)
    return rows.filter(archived_at__isnull=True).update(archived_at=timezone.now())


def mark_all_read(user) -> int:
    from .models import NotificationRecipient

    if current_context().organization_id is None:
        raise MissingTenantContext("Inbox changes happen inside the row's organization.")
    return NotificationRecipient.objects.filter(user=user, read_at__isnull=True).update(read_at=timezone.now())


def open_target(user, row_id) -> str | None:
    """Mark the row read and return the target URL if the person can still read it, else None."""
    row = _own_rows_in_tenant(user, [row_id]).get()
    if row.read_at is None:
        mark_read(user, [row.pk])
    return destination(user, row)


def open_item(user, notification_id) -> str | None:
    """The ``{link}`` of a notification (#806): where it leads for ``user``, re-authorized now.

    Only someone who received it opens it: their inbox row of this notification, the unread row
    a newer notification of the same group took over, or a delivery (email or push) of it. The
    row, if any, is marked read. The destination is the item of *this* notification. Anyone else,
    and an item the person no longer reads, gets ``None``: the same answer as a missing one.
    """
    from .models import Notification, NotificationDelivery, NotificationRecipient

    if current_context().organization_id is None:
        raise MissingTenantContext("A notification opens inside its organization.")
    try:
        notification_id = UUID(str(notification_id))
    except (ValueError, TypeError):
        return None
    notification = Notification.objects.filter(pk=notification_id).first()
    if notification is None:
        return None
    rows = NotificationRecipient.objects.filter(user=user)
    row = rows.filter(notification_id=notification.pk).first()
    if row is None and notification.group_key:
        row = rows.filter(group_key=notification.group_key).order_by("-updated_at", "sysid").first()
    if row is None and not NotificationDelivery.objects.filter(notification=notification, user_id=user.pk).exists():
        return None
    if row is not None and row.read_at is None:
        mark_read(user, [row.pk])
    return destination(user, notification)


def destination(user, item) -> str | None:
    """Where a row or a notification leads (same fields: type, target, payload, organization)."""
    from django.core.exceptions import PermissionDenied
    from django.urls import reverse

    from skeleton.access.policies import require_record_action
    from skeleton.system.registry import get_spec

    row = item
    if not row.target_sysid:
        from .registry import get_notification_type

        try:
            resolver = get_notification_type(row.type_code).open_url
        except KeyError:
            return None
        # The type's own resolver re-authorizes the person before giving a link.
        return resolver(user, row) if resolver is not None else None
    try:
        model = get_spec(row.target_table_key).model
        record = model.objects.filter(pk=row.target_sysid).first()
        if record is None:
            return None
        require_record_action(record, user=user, action="read")
    except (KeyError, PermissionDenied):
        return None
    from skeleton.access.derivation import is_derived

    if is_derived(model):
        # A derived table has no generic page (#562): only the type's own link, never one that 404s.
        from .registry import get_notification_type

        try:
            resolver = get_notification_type(row.type_code).open_url
        except KeyError:
            return None
        return resolver(user, row) if resolver is not None else None
    return reverse(
        "web:record",
        kwargs={"organization_id": row.organization_id, "table_key": row.target_table_key, "sysid": record.pk},
    )


def row_data(row) -> dict:
    """Presentation of one inbox row: the type label and the declared snapshot, never the target itself."""
    from .registry import get_notification_type

    try:
        label = get_notification_type(row.type_code).label
    except KeyError:
        label = row.type_code
    snapshot = dict(row.snapshot)
    # A row written before #783 has no text: its title is the label and its body the snapshot.
    title = row.title or str(label)
    body = row.body if row.title else " · ".join(str(value) for value in snapshot.values() if value not in (None, ""))
    return {
        "sysid": row.pk,
        "organization_id": row.organization_id,
        "type_code": row.type_code,
        "label": label,
        "title": title,
        "body": body,
        "snapshot": snapshot,
        "payload": dict(row.payload),
        "count": row.count,
        "read": row.read_at is not None,
        "archived": row.archived_at is not None,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def unread_rows(user, *, limit: int = 5) -> list[dict]:
    """The person's newest unread, unarchived rows of the current tenant (the internal home, #624)."""
    if current_context().organization_id is None:
        raise MissingTenantContext("The unread rows of the home need the tenant context.")
    return [row_data(row) for row in inbox_queryset(user).filter(read_at__isnull=True)[:limit]]
