"""Notices of the platform to its own administrators (#787): no organization, a box of their own.

``notify_platform`` records a notice of a type with ``audience="platform"`` (only the core
declares them, E023) outside any tenant and enqueues the global fan-out in the same transaction.
The fan-out gives a row to every active platform administrator and, when the type names a
``platform_capability``, to whoever holds it; each person's preferences decide the inbox and the
email (``daily``/``weekly`` count as immediate: the platform's notices ask for action now; there
is no push nor digest for them). The text is rendered per person, as in the organizations (#783).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any
from uuid import UUID

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.urls import reverse
from django.utils import timezone

FANOUT_JOB = "notifications.platform_fanout"
EMAIL_JOB = "notifications.platform_email"
RETENTION_JOB = "notifications.platform_retention"
MAX_RECIPIENTS = 500
OPEN_EMAIL = ("pending", "retrying")

logger = logging.getLogger("skeleton.notifications")


class NotAPlatformType(ValueError):
    """The type is unknown or not addressed to the platform."""


def _kind(type_code: str) -> Any:
    from .registry import get_notification_type

    try:
        kind = get_notification_type(type_code)
    except KeyError:
        raise NotAPlatformType(type_code) from None
    if kind.audience != "platform":
        raise NotAPlatformType(type_code)
    return kind


def notify_platform(type_code: str, *, payload: dict[str, Any] | None = None, dedupe_key: str | None = None) -> Any:
    """Record a platform notice and enqueue its fan-out; a repeated ``dedupe_key`` returns the existing one."""
    from django.core.exceptions import ValidationError
    from django.utils.translation import gettext

    from skeleton.jobs.services import enqueue
    from skeleton.tenancy.context import current_context

    from .models import PlatformNotification
    from .services import _scalar

    if current_context().organization_id is not None:
        raise RuntimeError("Platform notifications are global.")
    kind = _kind(type_code)
    values = dict(payload or {})
    undeclared = set(values) - set(kind.payload_fields)
    if undeclared:
        raise ValidationError(
            {"payload": gettext("Campos não declarados: %(value1)s.") % {"value1": sorted(undeclared)}}
        )
    values = {name: _scalar(name, value) for name, value in values.items()}
    if dedupe_key is not None and (not isinstance(dedupe_key, str) or not 1 <= len(dedupe_key) <= 200):
        raise ValidationError({"dedupe_key": gettext("Chave de 1 a 200 caracteres.")})
    group_key = ""
    if kind.group_key:
        try:
            group_key = kind.group_key.format(target="", **values)[:200]
        except (KeyError, IndexError, ValueError):
            group_key = ""
    with transaction.atomic():
        if dedupe_key is not None:
            existing = PlatformNotification.objects.filter(type_code=kind.code, dedupe_key=dedupe_key).first()
            if existing is not None:
                return existing
        notification = PlatformNotification(
            type_code=kind.code, payload=values, group_key=group_key, dedupe_key=dedupe_key
        )
        try:
            with transaction.atomic():
                notification.save()
        except IntegrityError:
            return PlatformNotification.objects.get(type_code=kind.code, dedupe_key=dedupe_key)
        enqueue(
            FANOUT_JOB,
            payload={"notification_id": str(notification.pk)},
            idempotency_key=f"platform-notification:{notification.pk}",
        )
    return notification


def platform_people(kind: Any) -> list[Any]:
    """Active human platform administrators and, with ``platform_capability``, its holders."""
    from skeleton.accounts.models import PlatformRoleAssignment, User
    from skeleton.accounts.platform import PLATFORM_ROLES

    people = User.objects.filter(is_active=True, deleted_at__isnull=True, account_type="human")
    query = Q(is_staff=True, is_superuser=True)
    if kind.platform_capability:
        roles = [role for role, capabilities in PLATFORM_ROLES.items() if kind.platform_capability in capabilities]
        holders = PlatformRoleAssignment.objects.filter(role__in=roles).values("user_id")
        query |= Q(pk__in=holders)
    return list(people.filter(query).select_related("profile").order_by("sysid")[: MAX_RECIPIENTS + 1])


def _still_recipient(user: Any, kind: Any) -> bool:
    from skeleton.accounts.platform import has_platform_capability, is_platform_admin

    if is_platform_admin(user):
        return True
    return bool(kind.platform_capability) and has_platform_capability(user, kind.platform_capability)


def platform_fan_out(*, notification_id: str) -> int:
    """Job handler (global system context): one row per person, then their emails."""
    from skeleton.jobs.services import enqueue

    from .fanout import delivery_enabled
    from .instance_settings import state_of
    from .models import PlatformNotification, PlatformNotificationRecipient
    from .preferences import rows_by_user, wants
    from .rendering import render_for_people

    notification = PlatformNotification.objects.filter(pk=notification_id).first()
    if notification is None or notification.fanned_out_at is not None:
        return 0
    kind = _kind(notification.type_code)
    created = 0
    if delivery_enabled(kind.code):
        people = platform_people(kind)
        if len(people) > MAX_RECIPIENTS:
            logger.warning(
                "platform_notification.too_many_recipients",
                extra={"event": "platform_notification.too_many_recipients"},
            )
            people = people[:MAX_RECIPIENTS]
        rows = rows_by_user([person.pk for person in people])
        chosen = state_of(kind.code).default_channels
        texts = render_for_people(
            kind,
            payload=notification.payload,
            snapshot={},
            users=people,
            organization_name="",
            sent_at=notification.created_at,
            link=item_url(notification.pk),
        )
        for person in people:
            in_app = wants(person.pk, kind, "in_app", rows, chosen) != "off"
            email = wants(person.pk, kind, "email", rows, chosen) != "off"
            if not (in_app or email):
                continue
            rendered = texts[person.pk]
            fields = {"title": rendered.title, "body": rendered.body, "payload": dict(notification.payload)}
            grouped = None
            if in_app and notification.group_key:
                grouped = PlatformNotificationRecipient.objects.filter(
                    user_id=person.pk,
                    group_key=notification.group_key,
                    in_app=True,
                    read_at__isnull=True,
                    archived_at__isnull=True,
                ).first()
            if grouped is not None:
                PlatformNotificationRecipient.objects.filter(pk=grouped.pk).update(
                    count=F("count") + 1, notification=notification, updated_at=timezone.now(), **fields
                )
                row_id = grouped.pk
            else:
                row = PlatformNotificationRecipient(
                    notification=notification,
                    user_id=person.pk,
                    type_code=kind.code,
                    group_key=notification.group_key,
                    in_app=in_app,
                    email_state="pending" if email else "",
                    **fields,
                )
                row.save()
                row_id = row.pk
                created += 1
            if email and grouped is None:
                enqueue(EMAIL_JOB, payload={"recipient_id": str(row_id)}, idempotency_key=f"platform-email:{row_id}")
    notification.fanned_out_at = timezone.now()
    notification.save(update_fields={"fanned_out_at"})
    return created


def _email_retry(row: Any, error_code: str) -> None:
    from skeleton.jobs.delivery import RetrySchedule
    from skeleton.jobs.services import enqueue

    from .channels.delivery import MAX_ATTEMPTS, RETRY_SCHEDULE

    row.email_attempts += 1
    if row.email_attempts < MAX_ATTEMPTS:
        delay = RetrySchedule(RETRY_SCHEDULE).delay_after(row.email_attempts) or timedelta(minutes=5)
        row.email_state, row.email_error_code = "retrying", error_code[:64]
        row.email_next_attempt_at = timezone.now() + delay
        row.save(update_fields={"email_state", "email_attempts", "email_error_code", "email_next_attempt_at"})
        enqueue(
            EMAIL_JOB,
            payload={"recipient_id": str(row.pk)},
            idempotency_key=f"platform-email:{row.pk}:{row.email_attempts}",
            run_after=row.email_next_attempt_at,
        )
        return
    row.email_state, row.email_error_code, row.email_next_attempt_at = "dead", error_code[:64], None
    row.save(update_fields={"email_state", "email_attempts", "email_error_code", "email_next_attempt_at"})


def _email_finish(row: Any, state: str, error_code: str = "") -> None:
    row.email_state, row.email_error_code, row.email_next_attempt_at = state, error_code, None
    if state == "sent":
        row.email_sent_at = timezone.now()
        row.email_attempts += 1
    row.save(
        update_fields={"email_state", "email_error_code", "email_next_attempt_at", "email_sent_at", "email_attempts"}
    )


def open_path(row: Any) -> str:
    return reverse("platform:notification-open", kwargs={"sysid": row.pk})


def item_url(notification_id: Any) -> str:
    """The ``{link}`` of a platform notice (#806): the same address for every administrator."""
    from .channels.delivery import absolute

    return absolute(reverse("platform:notification-item", kwargs={"notification_id": notification_id}))


def own_row_of_notification(user: Any, notification_id: Any) -> Any:
    """The person's row of a notice, or of the newer notice their unread row was grouped into."""
    from .models import PlatformNotification, PlatformNotificationRecipient

    rows = PlatformNotificationRecipient.objects.filter(user_id=user.pk)
    row = rows.filter(notification_id=notification_id).first()
    if row is None:
        notification = PlatformNotification.objects.filter(pk=notification_id).only("group_key").first()
        if notification is not None and notification.group_key:
            row = rows.filter(group_key=notification.group_key).order_by("-updated_at", "sysid").first()
    return row


def send_platform_email(*, recipient_id: str) -> None:
    """Job handler (global system context); never raises for a send failure."""
    from skeleton.accounts.models import User

    from .channels.delivery import absolute
    from .channels.email import build_message
    from .fanout import delivery_enabled
    from .models import PlatformNotificationRecipient
    from .preferences import effective
    from .texts import plain_html

    row = PlatformNotificationRecipient.objects.select_for_update().filter(pk=recipient_id).first()
    if row is None or row.email_state not in OPEN_EMAIL:
        return
    user = (
        User.objects.filter(pk=row.user_id, is_active=True, deleted_at__isnull=True).select_related("profile").first()
    )
    try:
        kind = _kind(row.type_code)
    except NotAPlatformType:
        _email_finish(row, "skipped", "unknown_type")
        return
    if user is None or not _still_recipient(user, kind):
        _email_finish(row, "skipped", "recipient_inactive")
        return
    if not delivery_enabled(kind.code):
        _email_finish(row, "skipped", "disabled")
        return
    if effective(user, kind.code, "email").mode == "off":
        _email_finish(row, "skipped", "opted_out")
        return
    context = {
        "label": kind.label,
        "title": row.title,
        "body": row.body,
        "body_html": plain_html(row.body, link=item_url(row.notification_id)),
        "type_code": kind.code,
        "snapshot": {},
        "payload": dict(row.payload),
        "created_at": row.created_at,
        "open_url": absolute(open_path(row)),
    }
    message = build_message(user, type_code=kind.code, context=context)
    try:
        if message.send(fail_silently=False) != 1:
            raise RuntimeError("Email backend did not accept the message.")
    except Exception as error:  # noqa: BLE001 - every send failure is recorded and retried
        _email_retry(row, f"send_{type(error).__name__}")
        return
    _email_finish(row, "sent")


# --------------------------------------------------------------------------- inbox


def is_platform_member(user: Any) -> bool:
    """Whoever administers the platform or holds any of its roles has a platform inbox."""
    from skeleton.accounts.platform import platform_capabilities

    return bool(platform_capabilities(user))


def inbox_queryset(user: Any, *, view: str = "unread") -> Any:
    from .models import PlatformNotificationRecipient

    rows = PlatformNotificationRecipient.objects.filter(user_id=user.pk, in_app=True)
    if view == "archived":
        return rows.filter(archived_at__isnull=False).order_by("-updated_at", "sysid")
    rows = rows.filter(archived_at__isnull=True)
    if view == "unread":
        rows = rows.filter(read_at__isnull=True)
    return rows.order_by("-updated_at", "sysid")


def unread_count(user: Any) -> int:
    if user is None or not getattr(user, "is_authenticated", False) or not is_platform_member(user):
        return 0
    return inbox_queryset(user, view="unread").count()


def own_row(user: Any, sysid: Any) -> Any:
    from .models import PlatformNotificationRecipient

    try:
        UUID(str(sysid))
    except ValueError:
        return None
    return PlatformNotificationRecipient.objects.filter(pk=sysid, user_id=user.pk, in_app=True).first()


def change(user: Any, sysid: Any, action: str) -> bool:
    row = own_row(user, sysid)
    if row is None:
        return False
    now = timezone.now()
    if action == "read" and row.read_at is None:
        row.read_at = now
        row.save(update_fields={"read_at"})
    elif action == "unread" and row.read_at is not None:
        row.read_at = None
        row.save(update_fields={"read_at"})
    elif action == "archive" and row.archived_at is None:
        row.archived_at = now
        row.save(update_fields={"archived_at"})
    return True


def read_all(user: Any) -> int:
    from .models import PlatformNotificationRecipient

    return PlatformNotificationRecipient.objects.filter(
        user_id=user.pk, in_app=True, read_at__isnull=True, archived_at__isnull=True
    ).update(read_at=timezone.now())


def destination(user: Any, row: Any) -> str | None:
    """Where the notice leads, re-authorized by the type; ``None`` keeps the person on the notice."""
    try:
        kind = _kind(row.type_code)
    except NotAPlatformType:
        return None
    if kind.open_url is None or not _still_recipient(user, kind):
        return None
    try:
        return kind.open_url(user, row)
    except Exception:  # noqa: BLE001 - a broken link leaves the person on the notice page
        return None


def retention_days(name: str, default: int) -> int:
    return int(getattr(settings, name, default) or default)


def purge_platform_notifications(*, now: Any = None) -> dict[str, int]:
    """Job handler (global): the same deadlines as the organizations' inbox rows (#787)."""
    from .models import PlatformNotification, PlatformNotificationRecipient

    now = now or timezone.now()
    read = now - timedelta(days=retention_days("SKELETON_NOTIFICATION_READ_RETENTION_DAYS", 90))
    unread = now - timedelta(days=retention_days("SKELETON_NOTIFICATION_UNREAD_RETENTION_DAYS", 180))
    open_email = Q(email_state__in=OPEN_EMAIL)
    old_read = PlatformNotificationRecipient.objects.filter(
        (Q(read_at__lt=read) | Q(archived_at__lt=read) | Q(in_app=False, updated_at__lt=read)) & ~open_email
    )
    old_unread = PlatformNotificationRecipient.objects.filter(updated_at__lt=unread)
    rows = old_read.delete()[0] + old_unread.delete()[0]
    orphans = PlatformNotification.objects.filter(created_at__lt=unread, recipients__isnull=True).delete()[0]
    return {"rows": rows, "notifications": orphans}


def forget_platform_person(user_id: Any) -> int:
    """Erasure of one person (#191): their platform inbox rows."""
    from .models import PlatformNotificationRecipient

    return PlatformNotificationRecipient.objects.filter(user_id=user_id).delete()[0]


def register_platform_jobs() -> None:
    from datetime import time

    from skeleton.jobs.periodic import register_periodic
    from skeleton.jobs.registry import JobType, register_job_type

    register_job_type(
        JobType(
            FANOUT_JOB,
            platform_fan_out,
            {"notification_id": "uuid"},
            run_as="system",
            scope="global",
            owner_app="sk_notifications",
        )
    )
    register_job_type(
        JobType(
            EMAIL_JOB,
            send_platform_email,
            {"recipient_id": "uuid"},
            run_as="system",
            scope="global",
            max_attempts=3,
            backoff=60.0,
            timeout_s=120,
            owner_app="sk_notifications",
            channel="email",
        )
    )
    register_job_type(
        JobType(
            RETENTION_JOB,
            purge_platform_notifications,
            {},
            run_as="system",
            scope="global",
            max_attempts=3,
            owner_app="sk_notifications",
        )
    )
    register_periodic(RETENTION_JOB, RETENTION_JOB, daily_at=time(3, 45), per_organization=False)
