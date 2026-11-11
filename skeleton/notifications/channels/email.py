"""Immediate notification email, sent by the ``notifications.email`` job (#54).

The job renders in the person's language and time zone and sends at least once: a send that
fails is retried by a new job on ``RETRY_SCHEDULE`` and the delivery ends ``dead``. Tokens
and secret links never go through this queue: allauth emails and invitations keep using
``schedule_mail``.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template import TemplateDoesNotExist
from django.template.loader import render_to_string
from django.utils import timezone, translation

from .delivery import OPEN_STATES, active_recipient, failed_attempt, finish, open_url, preferences_url, target_readable


def _candidates(type_code: str, stem: str, extension: str, language: str) -> list[str]:
    base = language.split("-")[0]
    names = []
    for folder in (type_code, "default"):
        names += [
            f"notifications/{folder}/{stem}.{language}.{extension}",
            f"notifications/{folder}/{stem}.{base}.{extension}",
            f"notifications/{folder}/{stem}.{extension}",
        ]
    return list(dict.fromkeys(names))


def _render(type_code: str, stem: str, extension: str, language: str, context: dict[str, Any]) -> str | None:
    try:
        return render_to_string(_candidates(type_code, stem, extension, language), context)
    except TemplateDoesNotExist:
        return None


def render_message(*, type_code: str, language: str, context: dict[str, Any]) -> tuple[str, str, str | None]:
    """``(subject, text, html)`` for ``notifications/<type>/{subject,body}[.<lang>].txt|html``."""
    subject = _render(type_code, "subject", "txt", language, context) or ""
    text = _render(type_code, "body", "txt", language, context) or ""
    html = _render(type_code, "body", "html", language, context)
    return " ".join(subject.split())[:200], text, html


def build_message(user: Any, *, type_code: str, context: dict[str, Any]) -> EmailMultiAlternatives:
    from skeleton.common.locale import preferences_for

    profile = getattr(user, "profile", None)
    preferences = preferences_for(user)
    language, zone = preferences.language, preferences.timezone
    unsubscribe = preferences_url()
    context = {**context, "preferences_url": unsubscribe, "user_display": getattr(profile, "display_name", "")}
    with translation.override(language), timezone.override(zone):
        subject, text, html = render_message(type_code=type_code, language=language, context=context)
    message = EmailMultiAlternatives(
        subject=subject,
        body=text,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[user.email],
        headers={"List-Unsubscribe": f"<{unsubscribe}>", "Auto-Submitted": "auto-generated"},
    )
    if html:
        message.attach_alternative(html, "text/html")
    return message


def _notification_context(delivery: Any, notification: Any) -> dict[str, Any]:
    from ..registry import get_notification_type

    try:
        label = get_notification_type(notification.type_code).label
    except KeyError:
        label = notification.type_code
    return {
        "label": label,
        "type_code": notification.type_code,
        "snapshot": dict(notification.snapshot),
        "payload": dict(notification.payload),
        "created_at": notification.created_at,
        "open_url": open_url(notification, delivery.user_id),
    }


def _still_wanted(user: Any, type_code: str) -> bool:
    from ..preferences import effective, known_type

    if known_type(type_code) is None:
        return False
    return effective(user, type_code, "email").mode == "immediate"


def send_email(*, delivery_id: str) -> None:
    """Job handler (system context of the organization); never raises for a send failure."""
    from ..models import NotificationDelivery

    delivery = NotificationDelivery.objects.select_for_update().filter(pk=delivery_id).first()
    if delivery is None or delivery.state not in OPEN_STATES:
        return  # Retention removed it, or an earlier run already decided it.
    user = active_recipient(delivery)
    if user is None:
        finish(delivery, "skipped", error_code="recipient_inactive")
        return
    if delivery.channel == "digest":
        from .digest import digest_message

        message = digest_message(delivery, user)
        if message is None:
            finish(delivery, "skipped", error_code="empty_digest")
            return
    else:
        notification = delivery.notification
        if notification is None or not _still_wanted(user, notification.type_code):
            finish(delivery, "skipped", error_code="opted_out")
            return
        if not target_readable(notification, user):
            finish(delivery, "skipped", error_code="target_unreadable")
            return
        message = build_message(
            user, type_code=notification.type_code, context=_notification_context(delivery, notification)
        )
    try:
        if message.send(fail_silently=False) != 1:
            raise RuntimeError("Email backend did not accept the message.")
    except Exception as error:  # noqa: BLE001 - every send failure is recorded and retried
        failed_attempt(delivery, f"send_{type(error).__name__}")
        return
    finish(delivery, "sent", attempts=delivery.attempts + 1, sent_at=timezone.now(), error_code="")
