"""The member limit reached (#698): one notification per day and limit to who manages members.

Sent when an admission takes the last seat (``used == limit``), in the transaction of the
admission (rolled back with it). Optional, without a target table: the fan-out re-checks
``members.manage`` for each person (``authorize``), and the payload carries only the numbers,
never a person.
"""

from __future__ import annotations

from typing import Any

from django.utils.translation import gettext_lazy

MEMBER_LIMIT_REACHED = "organization.member_limit_reached"
FIELDS = ("limit", "used")


def _authorize(user: Any, notification: Any) -> bool:
    from skeleton.access.capabilities import MEMBERS_MANAGE, has_capability

    return has_capability(user, MEMBERS_MANAGE)


def _open_url(user: Any, row: Any) -> str | None:
    from django.urls import reverse

    from skeleton.tenancy.context import current_context

    if not _authorize(user, row):
        return None
    return reverse(
        "web:access-list", kwargs={"organization_id": current_context().organization_id, "resource": "memberships"}
    )


def register_limit_notices() -> None:
    from skeleton.notifications.registry import NotificationType, register_notification_type

    register_notification_type(
        NotificationType(
            MEMBER_LIMIT_REACHED,
            str(gettext_lazy("Limite de membros atingido")),
            payload_fields=FIELDS,
            default_channels=("in_app", "email"),
            owner_app="sk_tenancy",
            open_url=_open_url,
            authorize=_authorize,
        )
    )


def member_admitted(organization_id: Any) -> None:
    """After a membership became active in the current organization: notify when it took the last seat."""
    from django.utils import timezone

    from skeleton.notifications.services import Recipients, notify
    from skeleton.system.limits import MEMBERS, limit_status
    from skeleton.tenancy.context import current_context

    if organization_id is None or current_context().organization_id != organization_id:
        return
    status = limit_status(MEMBERS)
    if status.limit is None or status.used != status.limit:
        return
    day = timezone.localdate().isoformat()
    notify(
        MEMBER_LIMIT_REACHED,
        recipients=Recipients(organization=True),
        payload={"limit": status.limit, "used": status.used},
        dedupe_key=f"member-limit:{status.limit}:{day}",
    )
