"""notify() validates in the caller's transaction and enqueues the fan-out; a rollback leaves nothing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.core.exceptions import ValidationError
from django.utils.translation import gettext

from skeleton.common.exceptions import MissingTenantContext
from skeleton.tenancy.context import current_context

from .registry import get_notification_type

FANOUT_JOB = "notifications.fanout"


@dataclass(frozen=True, slots=True)
class Recipients:
    """Who should receive a notification; resolved to people only at fan-out time."""

    users: tuple[UUID, ...] = ()
    groups: tuple[UUID, ...] = ()
    roles: tuple[UUID, ...] = ()
    organization: bool = False

    def as_json(self) -> dict[str, Any]:
        return {
            "users": sorted(str(item) for item in self.users),
            "groups": sorted(str(item) for item in self.groups),
            "roles": sorted(str(item) for item in self.roles),
            "organization": self.organization,
        }


def _uuids(values: Any, name: str) -> tuple[UUID, ...]:
    try:
        return tuple(UUID(str(getattr(value, "pk", value))) for value in values)
    except (ValueError, TypeError, AttributeError):
        raise ValidationError({"recipients": gettext("%(name)s: identificador inválido.") % {"name": name}}) from None


def _scalar(name: str, value: Any) -> Any:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, UUID | Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    raise ValidationError({"payload": f"{name}: use valores escalares."})


def _validate_recipients(recipients: Recipients) -> Recipients:
    from skeleton.access.models import AccessGroup, Role
    from skeleton.tenancy.models import Membership

    if not isinstance(recipients, Recipients):
        raise ValidationError({"recipients": gettext("Use Recipients.")})
    users, groups, roles = (
        _uuids(recipients.users, "users"),
        _uuids(recipients.groups, "groups"),
        _uuids(recipients.roles, "roles"),
    )
    if not (users or groups or roles or recipients.organization):
        raise ValidationError({"recipients": gettext("Informe ao menos um destinatário.")})
    # Managers are tenant-filtered: an identifier of another organization is simply not found.
    if len(set(groups)) != AccessGroup.objects.filter(pk__in=groups).count():
        raise ValidationError({"recipients": gettext("Grupo indisponível nesta organização.")})
    if len(set(roles)) != Role.objects.filter(pk__in=roles).count():
        raise ValidationError({"recipients": gettext("Papel indisponível nesta organização.")})
    if len(set(users)) != Membership.objects.filter(user_id__in=users, is_active=True).count():
        raise ValidationError({"recipients": gettext("Pessoa sem vínculo ativo nesta organização.")})
    return Recipients(users, groups, roles, bool(recipients.organization))


def notify(
    type_code: str,
    *,
    recipients: Recipients,
    target: Any = None,
    payload: dict[str, Any] | None = None,
    dedupe_key: str | None = None,
):
    """Record a notification and enqueue its fan-out in the current tenant transaction.

    A repeated ``dedupe_key`` for the same type returns the existing notification.
    """
    from django.db import IntegrityError, transaction

    from skeleton.jobs.services import enqueue
    from skeleton.system.registry import get_spec

    from .models import Notification

    context = current_context()
    if context.organization_id is None:
        raise MissingTenantContext("Notifications are created inside a tenant context.")
    try:
        kind = get_notification_type(type_code)
    except KeyError:
        raise ValidationError({"type_code": gettext("Tipo de notificação desconhecido.")}) from None
    payload = dict(payload or {})
    undeclared = set(payload) - set(kind.payload_fields)
    if undeclared:
        raise ValidationError(
            {"payload": gettext("Campos não declarados: %(value1)s.") % {"value1": sorted(undeclared)}}
        )
    values = {name: _scalar(name, value) for name, value in payload.items()}
    snapshot: dict[str, Any] = {}
    target_sysid = None
    if kind.target_table_key:
        if target is None or target._meta.label_lower != kind.target_table_key:
            raise ValidationError({"target": gettext("Alvo obrigatório da tabela declarada.")})
        if getattr(target, "organization_id", None) != context.organization_id:
            raise ValidationError({"target": gettext("O alvo pertence a outra organização.")})
        read_fields = set(get_spec(kind.target_table_key).read_fields)
        for name in kind.snapshot_fields:
            if name in read_fields:
                snapshot[name] = _scalar(name, getattr(target, name))
        target_sysid = target.pk
    elif target is not None:
        raise ValidationError({"target": gettext("Este tipo não tem alvo.")})
    resolved = _validate_recipients(recipients)
    if dedupe_key is not None and (not isinstance(dedupe_key, str) or not 1 <= len(dedupe_key) <= 200):
        raise ValidationError({"dedupe_key": gettext("Chave de 1 a 200 caracteres.")})
    group_key = ""
    if kind.group_key:
        try:
            group_key = kind.group_key.format(target=target_sysid or "", **values)[:200]
        except (KeyError, IndexError, ValueError):
            raise ValidationError({"payload": gettext("Campos insuficientes para o agrupamento.")}) from None
    notification = Notification(
        organization_id=context.organization_id,
        type_code=kind.code,
        actor_id=context.actor,
        target_table_key=kind.target_table_key,
        target_sysid=target_sysid,
        payload=values,
        snapshot=snapshot,
        recipients=resolved.as_json(),
        group_key=group_key,
        dedupe_key=dedupe_key,
    )
    try:
        with transaction.atomic(using=context.using):
            notification.save(using=context.using)
    except IntegrityError:
        if dedupe_key is None:
            raise
        return Notification.objects.get(type_code=kind.code, dedupe_key=dedupe_key)
    enqueue(
        FANOUT_JOB,
        organization_id=context.organization_id,
        payload={"notification_id": str(notification.pk), "offset": 0},
        idempotency_key=f"notification:{notification.pk}:0",
    )
    return notification


def register_notification_jobs() -> None:
    from skeleton.jobs.registry import JobType, register_job_type

    from .fanout import fan_out

    register_job_type(
        JobType(
            FANOUT_JOB,
            fan_out,
            {"notification_id": "uuid", "offset": "int"},
            run_as="system",
            owner_app="sk_notifications",
        )
    )
    from .channels.delivery import EMAIL_JOB
    from .channels.email import send_email

    register_job_type(
        JobType(
            EMAIL_JOB,
            send_email,
            {"delivery_id": "uuid"},
            run_as="system",
            # Crashes only: send failures never raise and follow RETRY_SCHEDULE through new jobs.
            max_attempts=3,
            backoff=60.0,
            timeout_s=120,
            owner_app="sk_notifications",
        )
    )
    from .channels.delivery import PUSH_JOB
    from .channels.push import send_push

    register_job_type(
        JobType(
            PUSH_JOB,
            send_push,
            {"delivery_id": "uuid"},
            run_as="system",
            # Crashes only: push outcomes never raise and follow RETRY_SCHEDULE through new jobs.
            max_attempts=3,
            backoff=60.0,
            timeout_s=120,
            owner_app="sk_notifications",
        )
    )
    from datetime import timedelta

    from skeleton.jobs.periodic import register_periodic

    from .channels.digest import DIGEST_JOB, run_digest

    register_job_type(
        JobType(DIGEST_JOB, run_digest, {}, run_as="system", max_attempts=3, owner_app="sk_notifications")
    )
    register_periodic(DIGEST_JOB, DIGEST_JOB, every=timedelta(hours=1))
    from datetime import time

    from .retention import RETENTION_JOB, purge_expired_notifications

    register_job_type(
        JobType(
            RETENTION_JOB,
            purge_expired_notifications,
            {},
            run_as="system",
            max_attempts=3,
            owner_app="sk_notifications",
        )
    )
    register_periodic(RETENTION_JOB, RETENTION_JOB, daily_at=time(3, 30))
