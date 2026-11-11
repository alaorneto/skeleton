"""Daily and weekly email digests of unread inbox rows (#55).

The periodic job ``notifications.digest`` runs every hour per organization, in its own
transaction. It picks the people whose digest hour (their time zone; weekly on Mondays) is
the current one and gathers their unread, unarchived rows of digest types not yet sent in a
digest. One ``NotificationDelivery`` per person and local hour (``dedupe_key``) makes a
second run of the same window a no-op; the rows are marked ``digest_sent_at`` so they never
join another digest. An empty digest is never created nor sent. A person in N organizations
receives N digests (accepted in this phase).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils import timezone

from .delivery import absolute, create_delivery

DIGEST_JOB = "notifications.digest"
MAX_ITEMS = 50
WEEKLY_DAY = 0  # Monday


def _local(now: datetime, zone: str) -> datetime:
    try:
        return now.astimezone(ZoneInfo(zone))
    except (ZoneInfoNotFoundError, ValueError):
        return now.astimezone(ZoneInfo("UTC"))


def due(mode: str, digest_hour: int, local: datetime) -> bool:
    """Whether a digest preference fires at this local time."""
    if local.hour != digest_hour:
        return False
    if mode == "daily":
        return True
    return mode == "weekly" and local.weekday() == WEEKLY_DAY


def _digest_people(organization_id: Any) -> list[Any]:
    from skeleton.tenancy.models import Membership

    from ..models import UserNotificationPreference
    from ..preferences import DIGEST_MODES

    candidates = UserNotificationPreference.objects.filter(channel="email", mode__in=DIGEST_MODES).values("user_id")
    return list(
        Membership.objects.filter(
            organization_id=organization_id,
            is_active=True,
            user__is_active=True,
            user__account_type="human",
            user_id__in=candidates,
        )
        .order_by("user_id")
        .values_list("user_id", flat=True)
    )


def run_digest(*, now: datetime | None = None) -> int:
    """Job handler (periodic, system context of one organization); returns digests created."""
    from skeleton.accounts.models import UserProfile
    from skeleton.tenancy.context import current_context

    from ..models import NotificationRecipient
    from ..preferences import resolve, rows_by_user
    from ..registry import all_notification_types

    now = now or timezone.now()
    organization_id = current_context().organization_id
    people = _digest_people(organization_id)
    rows = rows_by_user(people)
    zones = dict(UserProfile.objects.filter(user_id__in=people).values_list("user_id", "timezone"))
    kinds = all_notification_types()
    created = 0
    for user_id in people:
        local = _local(now, zones.get(user_id, "UTC"))
        types = []
        for kind in kinds:
            effective = resolve(kind, "email", rows.get(user_id, {}))
            if due(effective.mode, effective.digest_hour, local):
                types.append(kind.code)
        if not types:
            continue
        pending = (
            NotificationRecipient.objects.filter(
                organization_id=organization_id,
                user_id=user_id,
                type_code__in=types,
                read_at__isnull=True,
                archived_at__isnull=True,
                digest_sent_at__isnull=True,
            )
            .order_by("-updated_at", "sysid")
            .values_list("pk", flat=True)[:MAX_ITEMS]
        )
        items = list(pending)
        if not items:
            continue  # An empty digest is never sent.
        delivery = create_delivery(
            organization_id=organization_id,
            user_id=user_id,
            channel="digest",
            dedupe_key=f"digest:{user_id}:{local:%Y%m%d%H}",
            items=items,
        )
        if delivery is None:
            continue
        NotificationRecipient.objects.filter(pk__in=items).update(digest_sent_at=now)
        created += 1
    return created


def digest_message(delivery: Any, user: Any) -> Any | None:
    """The digest email, or ``None`` when every item was read, archived or left the digest."""
    from django.urls import reverse

    from ..models import NotificationRecipient
    from ..preferences import DIGEST_MODES, known_type, own_rows, resolve
    from ..registry import get_notification_type
    from .email import build_message

    preferences = own_rows(user)
    entries = []
    for row in NotificationRecipient.objects.filter(
        pk__in=delivery.items, user_id=user.pk, read_at__isnull=True, archived_at__isnull=True
    ).order_by("-updated_at", "sysid"):
        kind = known_type(row.type_code)
        if kind is None or resolve(kind, "email", preferences).mode not in DIGEST_MODES:
            continue
        entries.append(
            {
                "label": get_notification_type(row.type_code).label,
                "snapshot": dict(row.snapshot),
                "count": row.count,
                "updated_at": row.updated_at,
                "open_url": absolute(
                    reverse("notifications:open", kwargs={"organization_id": row.organization_id, "sysid": row.pk})
                ),
            }
        )
    if not entries:
        return None
    inbox = absolute(reverse("notifications:organization", kwargs={"organization_id": delivery.organization_id}))
    return build_message(
        user,
        type_code="digest",
        context={"entries": entries, "total": len(entries), "inbox_url": inbox, "label": "Resumo de notificações"},
    )
