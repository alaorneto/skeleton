"""Fan-out job: one row per person authorized at send time.

Recipients are a snapshot of that moment: someone removed from a group before
the fan-out receives nothing, and later group changes never rewrite history.
Each person must be able to read the target when the row is created.
"""

from __future__ import annotations

from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.db.models import F, Q
from django.utils import timezone

BATCH_SIZE = 10_000


def resolve_people(notification) -> list[UUID]:
    """Active members named directly or through groups, roles or the whole organization, minus the actor."""
    from skeleton.access.models import GroupMember, GroupRole, MemberRole
    from skeleton.tenancy.models import Membership

    spec = notification.recipients
    # People only: a service account (#44) has no inbox.
    active = Membership.objects.filter(
        is_active=True, user__is_active=True, user__deleted_at__isnull=True, user__account_type="human"
    )
    if spec.get("organization"):
        memberships = active
    else:
        groups = spec.get("groups", [])
        roles = spec.get("roles", [])
        in_groups = GroupMember.objects.filter(group_id__in=groups, group__deleted_at__isnull=True).values(
            "membership_id"
        )
        direct_roles = MemberRole.objects.filter(role_id__in=roles, role__deleted_at__isnull=True).values(
            "membership_id"
        )
        role_groups = GroupRole.objects.filter(
            role_id__in=roles, role__deleted_at__isnull=True, group__deleted_at__isnull=True
        ).values("group_id")
        via_role_groups = GroupMember.objects.filter(group_id__in=role_groups).values("membership_id")
        memberships = active.filter(
            Q(user_id__in=spec.get("users", []))
            | Q(pk__in=in_groups)
            | Q(pk__in=direct_roles)
            | Q(pk__in=via_role_groups)
        )
    people = set(memberships.values_list("user_id", flat=True))
    people.discard(notification.actor_id)
    return sorted(people, key=str)


def _target(notification):
    if not notification.target_sysid:
        return None
    from skeleton.system.registry import get_spec

    try:
        model = get_spec(notification.target_table_key).model
    except KeyError:
        return False
    return model.objects.filter(pk=notification.target_sysid).first() or False


def _authorized(people: list[UUID], target) -> list[UUID]:
    """Keep only people who can read the target now; no target means no authorization to check."""
    if target is None:
        return people
    if target is False:
        return []  # The target disappeared (deleted or retired): nobody is notified about it.
    from skeleton.access.policies import require_record_action
    from skeleton.accounts.models import User

    allowed = []
    for user in User.objects.filter(pk__in=people).order_by("sysid"):
        try:
            require_record_action(target, user=user, action="read")
        except PermissionDenied:
            continue
        allowed.append(user.pk)
    return allowed


def _type_authorized(notification, people: list[UUID]) -> list[UUID]:
    """The type's own re-authorization of each person (``NotificationType.authorize``), if any."""
    from skeleton.accounts.models import User

    from .registry import get_notification_type

    try:
        check = get_notification_type(notification.type_code).authorize
    except KeyError:
        return []
    if check is None:
        return people
    return [user.pk for user in User.objects.filter(pk__in=people).order_by("sysid") if check(user, notification)]


def _display(notification) -> dict:
    return {
        "type_code": notification.type_code,
        "target_table_key": notification.target_table_key,
        "target_sysid": notification.target_sysid,
        "snapshot": dict(notification.snapshot),
        "payload": dict(notification.payload),
    }


def fan_out(*, notification_id: str, offset: int) -> int:
    """Job handler (system context of the organization): one batch of at most BATCH_SIZE people."""
    from skeleton.jobs.services import enqueue

    from .models import Notification, NotificationRecipient

    notification = Notification.objects.filter(pk=notification_id).first()
    if notification is None or notification.fanned_out_at is not None:
        return 0
    people = resolve_people(notification)
    authorized = _authorized(people[offset : offset + BATCH_SIZE], _target(notification))
    authorized = _type_authorized(notification, authorized)
    batch, channels = _channels(notification, authorized)
    grouped = {}
    if notification.group_key:
        grouped = dict(
            NotificationRecipient.objects.filter(
                user_id__in=batch, group_key=notification.group_key, read_at__isnull=True, archived_at__isnull=True
            ).values_list("user_id", "pk")
        )
    if grouped:
        NotificationRecipient.objects.filter(pk__in=grouped.values()).update(
            count=F("count") + 1, notification=notification, **_display(notification)
        )
    NotificationRecipient.objects.bulk_create(
        [
            NotificationRecipient(
                organization_id=notification.organization_id,
                notification=notification,
                user_id=user,
                group_key=notification.group_key,
                **_display(notification),
            )
            for user in batch
            if user not in grouped
        ]
    )
    _schedule_channels(notification, channels)
    following = offset + BATCH_SIZE
    if following < len(people):
        enqueue(
            "notifications.fanout",
            organization_id=notification.organization_id,
            payload={"notification_id": str(notification.pk), "offset": following},
            idempotency_key=f"notification:{notification.pk}:{following}",
        )
    else:
        notification.fanned_out_at = timezone.now()
        notification.save(update_fields={"fanned_out_at"})
    return len(authorized)


def _channels(notification, people: list[UUID]) -> tuple[list[UUID], dict[str, list[UUID]]]:
    """Who gets an inbox row and who gets an immediate email or push, by their preferences (#53)."""
    from .preferences import rows_by_user, wants
    from .registry import get_notification_type

    try:
        kind = get_notification_type(notification.type_code)
    except KeyError:
        return people, {}
    rows = rows_by_user(people)
    inbox = [user for user in people if wants(user, kind, "in_app", rows) != "off"]
    from .channels.push import push_configured

    names = ("email", "push") if push_configured() else ("email",)
    channels = {
        channel: [user for user in people if wants(user, kind, channel, rows) == "immediate"] for channel in names
    }
    return inbox, channels


def _schedule_channels(notification, channels: dict[str, list[UUID]]) -> None:
    from .channels.delivery import create_delivery

    for channel, people in channels.items():
        for user in people:
            create_delivery(
                organization_id=notification.organization_id,
                notification_id=notification.pk,
                user_id=user,
                channel=channel,
                dedupe_key=f"{channel}:{notification.pk}:{user}",
            )
