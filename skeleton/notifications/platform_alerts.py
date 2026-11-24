"""Automatic notices of the platform to its administrators (#788).

A global periodic job (``notifications.platform_watch``, every 15 minutes) reads only counts and
states and calls :func:`skeleton.notifications.platform.notify_platform`; the ``dedupe_key`` of
each notice holds its window, so a situation is told once per window. Each check runs on its own:
one that fails is logged and the others go on. Without a live worker no job runs: "no worker"
stays with the health probe and ``check``. The payloads carry numbers, codes and identifiers only,
never a name, an email, a secret or an error message.

A new erasure request is told at once, in the request that files it (``request_erasure``).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext_lazy

WATCH_JOB = "notifications.platform_watch"
JOBS_DEAD = "platform.jobs_dead"
QUEUE_LAGGING = "platform.queue_lagging"
UPDATE_AVAILABLE = "platform.update_available"
OPERATION_FAILED = "platform.operation_failed"
ERASURE_REQUESTED = "platform.erasure_requested"
ORGANIZATION_SUSPENDED = "platform.organization_suspended"
FAILED_OPERATION_STATES = ("failed", "interrupted", "restored")
DEAD_BANDS = (1000, 100, 10, 1)

logger = logging.getLogger("skeleton.notifications")


def _url(name: str, capability: str = "", **kwargs: Any):
    def open_url(user: Any, row: Any) -> str | None:
        from django.urls import NoReverseMatch, reverse

        from skeleton.accounts.platform import has_platform_capability, is_platform_admin

        if not (is_platform_admin(user) or (capability and has_platform_capability(user, capability))):
            return None
        values = {key: row.payload.get(field) for key, field in kwargs.items()}
        if any(value in (None, "") for value in values.values()):
            return None
        try:
            return reverse(name, kwargs=values)
        except NoReverseMatch:
            return None

    return open_url


def register_platform_alerts() -> None:
    from skeleton.notifications.registry import NotificationField, NotificationType, register_notification_type

    platform = {"context": "platform", "audience": "platform"}
    number = "number"
    register_notification_type(
        NotificationType(
            JOBS_DEAD,
            gettext_lazy("Jobs que falharam"),
            payload_fields=("count",),
            fields=(NotificationField("count", gettext_lazy("Quantidade"), kind=number),),
            default_channels=("in_app", "email"),
            owner_app="sk_jobs",
            description=gettext_lazy("Quando há jobs que esgotaram as tentativas"),
            title=gettext_lazy("Há {count} jobs que falharam"),
            body=gettext_lazy("Abra Jobs para reenviar ou descartar."),
            open_url=_url("platform:jobs"),
            **platform,
        )
    )
    register_notification_type(
        NotificationType(
            QUEUE_LAGGING,
            gettext_lazy("Fila de jobs atrasada"),
            payload_fields=("minutes", "due"),
            fields=(
                NotificationField("minutes", gettext_lazy("Minutos de atraso"), kind=number),
                NotificationField("due", gettext_lazy("Jobs na fila"), kind=number),
            ),
            default_channels=("in_app", "email"),
            owner_app="sk_jobs",
            description=gettext_lazy("Quando o job mais antigo da fila espera além do limite"),
            title=gettext_lazy("A fila de jobs está {minutes} min atrasada"),
            body=gettext_lazy("{due} jobs aguardam execução."),
            open_url=_url("platform:jobs"),
            **platform,
        )
    )
    register_notification_type(
        NotificationType(
            UPDATE_AVAILABLE,
            gettext_lazy("Atualizações disponíveis"),
            payload_fields=("core", "apps"),
            fields=(
                NotificationField("core", gettext_lazy("Versão do núcleo")),
                NotificationField("apps", gettext_lazy("Aplicativos"), kind=number),
            ),
            default_channels=("in_app",),
            owner_app="sk_system",
            platform_capability="updates.operate",
            description=gettext_lazy("Quando há versão nova do núcleo ou de aplicativos"),
            title=gettext_lazy("Atualizações disponíveis"),
            body=gettext_lazy("Núcleo: {core}. Aplicativos com atualização: {apps}."),
            open_url=_url("platform:updates", "updates.operate"),
            **platform,
        )
    )
    register_notification_type(
        NotificationType(
            OPERATION_FAILED,
            gettext_lazy("Atualização que não terminou"),
            payload_fields=("operation",),
            default_channels=("in_app", "email"),
            owner_app="sk_system",
            platform_capability="updates.operate",
            description=gettext_lazy("Quando uma operação de atualização falha, é interrompida ou revertida"),
            title=gettext_lazy("Uma operação de atualização não terminou"),
            body=gettext_lazy("Abra a operação para ver o que aconteceu."),
            open_url=_url("platform:updates-operation", "updates.operate", operation_id="operation"),
            **platform,
        )
    )
    register_notification_type(
        NotificationType(
            ERASURE_REQUESTED,
            gettext_lazy("Pedido de eliminação de dados"),
            payload_fields=("request",),
            default_channels=("in_app", "email"),
            owner_app="sk_accounts",
            description=gettext_lazy("Quando uma pessoa pede a eliminação dos próprios dados"),
            title=gettext_lazy("Novo pedido de eliminação de dados"),
            body=gettext_lazy("Um pedido aguarda decisão em Pedidos de eliminação."),
            open_url=_url("platform:privacy"),
            **platform,
        )
    )
    register_notification_type(
        NotificationType(
            ORGANIZATION_SUSPENDED,
            gettext_lazy("Organização suspensa"),
            payload_fields=("organization",),
            default_channels=("in_app", "email"),
            owner_app="sk_billing",
            platform_capability="billing.operate",
            description=gettext_lazy("Quando a falta de pagamento suspende uma organização"),
            title=gettext_lazy("Uma organização foi suspensa por falta de pagamento"),
            open_url=_url("platform:organization-billing", "billing.operate", sysid="organization"),
            **platform,
        )
    )


def register_platform_watch() -> None:
    from skeleton.jobs.periodic import register_periodic
    from skeleton.jobs.registry import JobType, register_job_type

    register_job_type(
        JobType(
            WATCH_JOB, platform_watch, {}, run_as="system", scope="global", max_attempts=1, owner_app="sk_notifications"
        )
    )
    register_periodic(WATCH_JOB, WATCH_JOB, every=timedelta(minutes=15), per_organization=False)


# --------------------------------------------------------------------------- checks


def dead_band(count: int) -> int:
    """The largest of 1, 10, 100 and 1000 not above ``count`` (0 below 1)."""
    return next((band for band in DEAD_BANDS if count >= band), 0)


def check_dead_jobs(now: Any) -> None:
    from skeleton.jobs.models import Job

    from .platform import notify_platform

    count = Job.objects.filter(state=Job.State.DEAD).count()
    band = dead_band(count)
    if band:
        notify_platform(JOBS_DEAD, payload={"count": count}, dedupe_key=f"jobs-dead:{now:%Y%m%d}:{band}")


def check_queue(now: Any) -> None:
    from skeleton.jobs.operations import queue_health
    from skeleton.jobs.worker import maintenance_paused

    from .platform import notify_platform

    if maintenance_paused():
        return  # A queue paused by maintenance is late on purpose (#448).
    limit = int(getattr(settings, "SKELETON_PLATFORM_ALERT_QUEUE_LAG_S", 900) or 900)
    health = queue_health(now=now)
    if health.oldest_due_lag_s > limit:
        notify_platform(
            QUEUE_LAGGING,
            payload={"minutes": int(health.oldest_due_lag_s // 60), "due": health.due},
            dedupe_key=f"queue-lag:{now:%Y%m%d%H}",
        )


def check_updates(now: Any) -> None:
    from skeleton.system.releases import update_notice

    from .platform import notify_platform

    notice = update_notice()
    if not notice:
        return
    core = str(notice.get("core") or "—")[:40]
    apps = int(notice.get("apps") or 0)
    notify_platform(UPDATE_AVAILABLE, payload={"core": core, "apps": apps}, dedupe_key=f"update:{core}:{apps}")


def check_operations(now: Any) -> None:
    from skeleton.system.models import DeploymentOperation

    from .platform import notify_platform

    since = now - timedelta(hours=24)
    rows = DeploymentOperation.objects.filter(state__in=FAILED_OPERATION_STATES, updated_at__gte=since)
    for operation_id in rows.values_list("pk", flat=True):
        notify_platform(
            OPERATION_FAILED, payload={"operation": str(operation_id)}, dedupe_key=f"operation:{operation_id}"
        )


def check_suspended(now: Any) -> None:
    from skeleton.insights.models import OrganizationSnapshot

    from .platform import notify_platform

    rows = OrganizationSnapshot.objects.filter(is_active=True, billing__access_state="suspended")
    for organization_id in rows.values_list("subject_organization_id", flat=True):
        notify_platform(
            ORGANIZATION_SUSPENDED,
            payload={"organization": str(organization_id)},
            dedupe_key=f"org-suspended:{organization_id}:{now:%Y%m%d}",
        )


CHECKS = (
    ("jobs_dead", check_dead_jobs),
    ("queue_lag", check_queue),
    ("updates", check_updates),
    ("operations", check_operations),
    ("suspended", check_suspended),
)


def platform_watch(*, now: Any = None) -> list[str]:
    """Job handler (global system context): every check in its own savepoint; returns the failed ones."""
    from django.db import transaction

    if not getattr(settings, "SKELETON_PLATFORM_ALERTS_ENABLED", True):
        return []
    now = now or timezone.now()
    failed = []
    for name, check in CHECKS:
        try:
            with transaction.atomic():
                check(now)
        except Exception:  # noqa: BLE001 - one broken check never stops the others
            failed.append(name)
            logger.warning("platform_watch.failed", extra={"event": "platform_watch.failed", "reason_code": name})
    return failed


def erasure_requested(request_id: Any) -> None:
    """Called by ``request_erasure`` in its transaction (global context)."""
    from .platform import NotAPlatformType, notify_platform

    if not getattr(settings, "SKELETON_PLATFORM_ALERTS_ENABLED", True):
        return
    try:
        notify_platform(ERASURE_REQUESTED, payload={"request": str(request_id)}, dedupe_key=f"erasure:{request_id}")
    except NotAPlatformType:
        return  # Not registered (a test with an isolated registry).
