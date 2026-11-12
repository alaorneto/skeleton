"""Mentions in comments (#188): ``@[name](sysid)`` tokens that notify the person mentioned.

- The token carries the person's ``sysid``; the name is only the label the author's picker
  wrote. The server never resolves names into the text and never trusts the label.
- On save, every mentioned ``sysid`` must be an active member of the organization who reads
  the record; otherwise the comment is refused (400) and nothing is written.
- Each mentioned person gets one ``comments.mention`` notification per comment
  (``dedupe_key`` by comment and person): editing the comment notifies only people
  mentioned for the first time. Mentioning oneself notifies nobody.
- The fan-out re-checks, through ``NotificationType.authorize``, that the comment is still
  live and that the person still reads the record: revoked access stops the delivery.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.utils.translation import gettext, gettext_lazy

TYPE = "comments.mention"
MAX_MENTIONS = 20
TOKEN = re.compile(
    r"@\[([^\]\n<>]{1,100})\]\(([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\)", re.IGNORECASE
)


def mentioned_ids(body: str) -> list[UUID]:
    """The distinct sysids mentioned in a body, in order of first appearance."""
    seen: dict[UUID, None] = {}
    for match in TOKEN.finditer(body):
        seen.setdefault(UUID(match.group(2)), None)
    return list(seen)


def token(label: str, user_id: Any) -> str:
    clean = re.sub(r"[\]\[\n<>()]", "", str(label)).strip()[:100] or "pessoa"
    return f"@[{clean}]({UUID(str(user_id))})"


def reads(spec: Any, record_id: Any, user: Any) -> bool:
    from skeleton.access.policies import authorized_queryset

    try:
        return authorized_queryset(spec.model, user=user, action="read").filter(pk=record_id).exists()
    except PermissionDenied:
        return False


def _members(ids: list[UUID]) -> Any:
    from skeleton.accounts.models import User
    from skeleton.tenancy.models import Membership

    active = Membership.objects.filter(is_active=True, user_id__in=ids).values("user_id")
    return User.objects.filter(pk__in=active, is_active=True)


def validate(spec: Any, record: Any, body: str) -> list[UUID]:
    """The people the body mentions; refuses a mention of anyone who is not a reader member."""
    ids = mentioned_ids(body)
    if len(ids) > MAX_MENTIONS:
        raise ValidationError({"body": gettext("Mencione até %(count)d pessoas.") % {"count": MAX_MENTIONS}})
    if not ids:
        return []
    readers = {user.pk for user in _members(ids) if reads(spec, record.pk, user)}
    if set(ids) - readers:
        # One message for every failure: whether the person exists is not revealed.
        raise ValidationError({"body": gettext("Só é possível mencionar membros que leem este registro.")})
    return ids


def notify(spec: Any, record: Any, comment: Any, people: list[UUID]) -> None:
    from skeleton.notifications.services import Recipients
    from skeleton.notifications.services import notify as record_notification

    for person in people:
        record_notification(
            TYPE,
            recipients=Recipients(users=(person,)),
            payload={"comment": str(comment.pk), "table": spec.model._meta.label_lower, "record": str(record.pk)},
            dedupe_key=f"mention:{comment.pk}:{person}",
        )


def _target(notification: Any) -> tuple[Any, Any] | None:
    """The spec and the live comment of a mention notification, or None."""
    from skeleton.common.models import SecuredRecord
    from skeleton.system.registry import get_spec

    from .models import Comment

    payload = notification.payload
    try:
        spec = get_spec(payload.get("table", ""))
        comment = Comment.objects.filter(pk=payload.get("comment"), target_sysid=payload.get("record")).first()
    except (KeyError, ValidationError, ValueError):
        return None
    if comment is None or spec.comments is None or not issubclass(spec.model, SecuredRecord):
        return None
    return spec, comment


def authorize(user: Any, notification: Any) -> bool:
    """Fan-out re-authorization: the comment is live and the person still reads the record."""
    target = _target(notification)
    return target is not None and reads(target[0], target[1].target_sysid, user)


def open_url(user: Any, row: Any) -> str | None:
    """Where the notification leads: the record page, only while the person reads it."""
    from django.urls import NoReverseMatch, reverse

    target = _target(row)
    if target is None or not reads(target[0], target[1].target_sysid, user):
        return None
    try:
        return reverse(
            "web:record",
            kwargs={
                "organization_id": row.organization_id,
                "table_key": target[0].model._meta.label_lower,
                "sysid": target[1].target_sysid,
            },
        )
    except NoReverseMatch:
        return None


#: Members examined for the picker; each one costs an authorization query.
SCAN_LIMIT = 500


def _name(user: Any, profile: Any) -> str:
    """Display name without the email: members without ``members.view`` never see addresses."""
    name = (getattr(profile, "display_name", "") or "").strip() or f"{user.first_name} {user.last_name}".strip()
    return name or gettext("Membro")


def mentionable(spec: Any, record: Any, *, limit: int = 50) -> list[dict[str, Any]]:
    """Active members who read the record, with the token the picker inserts."""
    from skeleton.accounts.models import User, UserProfile
    from skeleton.tenancy.models import Membership

    ids = Membership.objects.filter(is_active=True).values("user_id")
    people = []
    users = list(User.objects.filter(pk__in=ids, is_active=True).order_by("sysid")[:SCAN_LIMIT])
    profiles = {profile.user_id: profile for profile in UserProfile.objects.filter(user__in=users)}
    for user in users:
        if not reads(spec, record.pk, user):
            continue
        name = _name(user, profiles.get(user.pk))
        people.append({"sysid": user.pk, "name": name, "token": token(name, user.pk)})
        if len(people) >= limit:
            break
    return people


def register_mention_type() -> None:
    from skeleton.notifications.registry import NotificationType, register_notification_type

    register_notification_type(
        NotificationType(
            TYPE,
            gettext_lazy("Menção em comentário"),
            payload_fields=("comment", "table", "record"),
            owner_app="sk_comments",
            open_url=open_url,
            authorize=authorize,
        )
    )
