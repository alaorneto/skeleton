"""Announcements of the platform to the organizations (#789).

The platform administrator writes a draft (title and text, who receives it, which organizations),
reviews it and sends it after a confirmation. Sending marks it ``sent`` (it never changes again)
and enqueues one global job that walks the chosen organizations 500 at a time, each one with a job
of its own that notifies inside that organization, in its system context and own transaction.
The text is plain, escaped and has no placeholder: it is the content of the notice
(``fixed_text``), so neither the platform's settings nor an organization rewrite it. The notice
is mandatory in the inbox; each person still chooses about the email.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext, gettext_lazy

from skeleton.common.exceptions import ConcurrentUpdate

TYPE = "platform.announcement"
BATCH_JOB = "notifications.announce_batch"
ANNOUNCE_JOB = "notifications.announce"
BATCH = 500
MAX_ORGANIZATIONS_LISTED = 1000


def _require_platform(actor: Any) -> None:
    from skeleton.accounts.platform import is_platform_admin

    if actor is not None and not is_platform_admin(actor):
        raise PermissionDenied("Platform administration required.")


def _authorize(user: Any, notification: Any) -> bool:
    from skeleton.access.policies import is_organization_admin

    return notification.payload.get("audience") != "admins" or is_organization_admin(user)


def register_announcement_type() -> None:
    from .registry import NotificationField, NotificationType, register_notification_type

    register_notification_type(
        NotificationType(
            TYPE,
            gettext_lazy("Comunicado da plataforma"),
            payload_fields=("announcement", "title", "body", "audience"),
            fields=(
                NotificationField("title", gettext_lazy("Título do comunicado")),
                NotificationField("body", gettext_lazy("Texto do comunicado")),
            ),
            default_channels=("in_app", "email"),
            mandatory=True,
            owner_app="sk_notifications",
            context="platform",
            fixed_text=True,
            description=gettext_lazy("Comunicado escrito pelo administrador da plataforma"),
            title="{title}",
            body="{body}",
            authorize=_authorize,
        )
    )


def _text(title: Any, body: Any) -> tuple[str, str]:
    """Plain text: the limits of every notice text, and braces are just characters.

    The text is the value of the ``{title}`` and ``{body}`` placeholders, never parsed itself; the
    braces are escaped only to reuse the checks of length, lines and control characters.
    """
    from .texts import validate

    errors: dict[str, Any] = {}
    clean: dict[str, str] = {}
    for name, value in (("title", title), ("body", body if body is not None else "")):
        if not isinstance(value, str):
            errors[name] = ValidationError(gettext("Informe um texto."))
            continue
        try:
            escaped = validate(value.replace("{", "{{").replace("}", "}}"), allowed=set(), kind=name)
            clean[name] = escaped.replace("{{", "{").replace("}}", "}")
        except ValidationError as error:
            errors[name] = error
    if errors:
        raise ValidationError(errors)
    return clean["title"], clean["body"]


def resolve_organizations(values: Any) -> list[str]:
    """Slugs or UUIDs of active organizations as UUID strings; an unknown one is an error that names it."""
    from skeleton.tenancy.models import Organization

    if not isinstance(values, list | tuple):
        raise ValidationError({"organizations": gettext("Informe as organizações.")})
    wanted = [str(value).strip() for value in values if str(value).strip()]
    if len(wanted) > MAX_ORGANIZATIONS_LISTED:
        raise ValidationError({"organizations": gettext("Escolha no máximo 1000 organizações, ou todas.")})
    keys: list[tuple[str, str]] = []
    for value in wanted:
        try:
            keys.append(("id", str(UUID(value))))
        except ValueError:
            keys.append(("slug", value.lower()))
    rows = Organization.objects.filter(is_active=True)
    by_id = {str(row.pk): row for row in rows.filter(pk__in=[key for kind, key in keys if kind == "id"])}
    by_slug = {row.slug: row for row in rows.filter(slug__in=[key for kind, key in keys if kind == "slug"])}
    chosen, missing = set(), []
    for value, (kind, key) in zip(wanted, keys, strict=True):
        organization = (by_id if kind == "id" else by_slug).get(key)
        if organization is None:
            missing.append(value)
        else:
            chosen.add(str(organization.pk))
    if missing:
        raise ValidationError(
            {
                "organizations": gettext("Organizações desconhecidas ou inativas: %(names)s.")
                % {"names": ", ".join(missing)}
            }
        )
    return sorted(chosen)


def _audience(value: Any) -> str:
    from .models import PlatformAnnouncement

    if value not in PlatformAnnouncement.Audience.values:
        raise ValidationError({"audience": gettext("Escolha quem recebe.")})
    return value


def create_announcement(*, actor: Any, title: str, body: str, audience: str, organizations: list[str]) -> Any:
    from .models import PlatformAnnouncement

    _require_platform(actor)
    title, body = _text(title, body)
    record = PlatformAnnouncement(
        title=title, body=body, audience=_audience(audience), organizations=resolve_organizations(organizations)
    )
    record.save()
    return record


def _draft(announcement_id: Any, expected_revision: Any) -> Any:
    from .models import PlatformAnnouncement

    record = PlatformAnnouncement.objects.select_for_update().filter(pk=announcement_id).first()
    if record is None:
        raise ConcurrentUpdate("The announcement is gone.")
    if type(expected_revision) is not int or record.revision != expected_revision:
        raise ConcurrentUpdate("The announcement changed.")
    if record.state != PlatformAnnouncement.State.DRAFT:
        raise ValidationError({"state": ValidationError(gettext("Um comunicado enviado não muda."), code="sent")})
    return record


def update_announcement(
    *,
    actor: Any,
    announcement_id: Any,
    expected_revision: int,
    title: str,
    body: str,
    audience: str,
    organizations: list[str],
) -> Any:
    _require_platform(actor)
    title, body = _text(title, body)
    audience = _audience(audience)
    chosen = resolve_organizations(organizations)
    with transaction.atomic():
        record = _draft(announcement_id, expected_revision)
        record.title, record.body, record.audience, record.organizations = title, body, audience, chosen
        record.save(update_fields={"title", "body", "audience", "organizations"})
    return record


def delete_announcement(*, actor: Any, announcement_id: Any, expected_revision: int) -> None:
    _require_platform(actor)
    with transaction.atomic():
        _draft(announcement_id, expected_revision).delete()


def targets(record: Any) -> list[str]:
    """The UUIDs the announcement goes to: the chosen ones still active, or every active organization."""
    from skeleton.tenancy.models import Organization

    rows = Organization.objects.filter(is_active=True)
    if record.organizations:
        rows = rows.filter(pk__in=record.organizations)
    return [str(pk) for pk in rows.order_by("pk").values_list("pk", flat=True)]


def send_announcement(*, actor: Any, announcement_id: Any, expected_revision: int) -> Any:
    from skeleton.jobs.services import enqueue

    _require_platform(actor)
    with transaction.atomic():
        record = _draft(announcement_id, expected_revision)
        chosen = targets(record)
        if not chosen:
            raise ValidationError({"organizations": gettext("Nenhuma organização ativa para receber.")})
        record.state = record.State.SENT
        record.sent_at = timezone.now()
        record.sent_by = getattr(actor, "pk", None)
        record.organization_count = len(chosen)
        record.save(update_fields={"state", "sent_at", "sent_by", "organization_count"})
        enqueue(
            BATCH_JOB,
            payload={"announcement": str(record.pk), "offset": 0},
            idempotency_key=f"announcement-batch:{record.pk}:0",
        )
    return record


def announce_batch(*, announcement: str, offset: int) -> int:
    """Global job: one ``notifications.announce`` per organization of this slice, then the next slice."""
    from skeleton.jobs.services import enqueue

    from .models import PlatformAnnouncement

    record = PlatformAnnouncement.objects.filter(pk=announcement, state=PlatformAnnouncement.State.SENT).first()
    if record is None:
        return 0
    chosen = targets(record)
    for organization_id in chosen[offset : offset + BATCH]:
        enqueue(
            ANNOUNCE_JOB,
            organization_id=organization_id,
            payload={"announcement": str(record.pk)},
            idempotency_key=f"announcement:{record.pk}",
        )
    following = offset + BATCH
    if following < len(chosen):
        enqueue(
            BATCH_JOB,
            payload={"announcement": str(record.pk), "offset": following},
            idempotency_key=f"announcement-batch:{record.pk}:{following}",
        )
    return min(BATCH, max(len(chosen) - offset, 0))


def announce(*, announcement: str) -> None:
    """Organization job (its system context): the notice of the announcement to its people."""
    from .models import PlatformAnnouncement
    from .services import Recipients, notify

    record = PlatformAnnouncement.objects.filter(pk=announcement, state=PlatformAnnouncement.State.SENT).first()
    if record is None:
        return
    notify(
        TYPE,
        recipients=Recipients(organization=True),
        payload={
            "announcement": str(record.pk),
            "title": record.title,
            "body": record.body,
            "audience": record.audience,
        },
        dedupe_key=f"announcement:{record.pk}",
    )


def register_announcement_jobs() -> None:
    from skeleton.jobs.registry import JobType, register_job_type

    register_job_type(
        JobType(
            BATCH_JOB,
            announce_batch,
            {"announcement": "uuid", "offset": "int"},
            run_as="system",
            scope="global",
            owner_app="sk_notifications",
        )
    )
    register_job_type(
        JobType(ANNOUNCE_JOB, announce, {"announcement": "uuid"}, run_as="system", owner_app="sk_notifications")
    )
