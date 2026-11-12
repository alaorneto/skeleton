"""Delivery log, manual resend, ping and retention of webhook deliveries (#65).

Resending creates a new delivery linked by ``resend_of``; the original row, its attempts and
outcome never change. A ping sends the reserved ``webhook.ping`` event, which carries no
record. Both require ``integrations.manage`` and the endpoint revision the caller saw (CAS),
and refuse a disabled endpoint. Retention is the only path that deletes delivery rows.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext

from skeleton.common.exceptions import ConcurrentUpdate

PING_EVENT = "webhook.ping"
RETENTION_JOB = "webhooks.retention"
DEFAULT_RETENTION_DAYS = 30
FINAL_STATES = ("succeeded", "failed", "unauthorized", "skipped")


def _endpoint_for_action(endpoint: Any, expected_revision: int) -> Any:
    from .models import WebhookEndpoint

    locked = WebhookEndpoint.objects.select_for_update().filter(pk=endpoint.pk).first()
    if locked is None:
        raise WebhookEndpoint.DoesNotExist()
    if locked.revision != expected_revision:
        raise ConcurrentUpdate("The webhook endpoint changed.")
    if not locked.is_enabled:
        raise ValidationError(
            {"endpoint": ValidationError(gettext("O webhook está desativado."), code="endpoint_disabled")}
        )
    return locked


def resend_delivery(delivery: Any, *, actor: Any, expected_revision: int) -> Any:
    """A new delivery with the same body and event, to the endpoint as it is now."""
    from .delivery import enqueue_attempt
    from .models import WebhookDelivery
    from .services import _lock

    with _lock(actor):
        endpoint = _endpoint_for_action(delivery.endpoint, expected_revision)
        retry = WebhookDelivery(
            organization_id=delivery.organization_id,
            endpoint=endpoint,
            event_id=delivery.event_id,
            event_type=delivery.event_type,
            table_key=delivery.table_key,
            record_sysid=delivery.record_sysid,
            payload=delivery.payload,
            # Always the first delivery of the event, so a resend is never itself referenced.
            resend_of_id=delivery.resend_of_id or delivery.pk,
        )
        retry.save()
        enqueue_attempt(retry, endpoint.principal_id)
        return retry


def ping_endpoint(endpoint: Any, *, actor: Any, expected_revision: int) -> Any:
    from .delivery import enqueue_attempt
    from .models import WebhookDelivery
    from .services import _lock

    with _lock(actor):
        locked = _endpoint_for_action(endpoint, expected_revision)
        event_id = uuid.uuid4()
        ping = WebhookDelivery(
            organization_id=locked.organization_id,
            endpoint=locked,
            event_id=event_id,
            event_type=PING_EVENT,
            payload={
                "id": str(event_id),
                "type": PING_EVENT,
                "version": 1,
                "occurred_at": timezone.now().isoformat(),
                "organization": str(locked.organization_id),
                "data": {},
            },
        )
        ping.save()
        enqueue_attempt(ping, locked.principal_id)
        return ping


def retention_days() -> int:
    return int(getattr(settings, "SKELETON_WEBHOOK_RETENTION_DAYS", DEFAULT_RETENTION_DAYS))


def purge_expired_deliveries(*, now: Any = None, created_before: Any = None) -> int:
    """Job handler (per organization, system context): the only delete path of deliveries.

    ``now`` and ``created_before`` exist for tests; the periodic job passes neither.
    """
    from .models import WebhookDelivery

    cutoff = (now or timezone.now()) - timedelta(days=retention_days())
    expired = WebhookDelivery.objects.filter(created_at__lt=cutoff, state__in=FINAL_STATES)
    if created_before is not None:
        expired = expired.filter(created_at__lt=created_before)
    # A live resend still points at its original (PROTECT): keep the original until both expire.
    kept = WebhookDelivery.objects.exclude(pk__in=expired.values("pk")).filter(resend_of__isnull=False)
    deleted = 0
    for batch in (expired.filter(resend_of__isnull=False), expired.exclude(pk__in=kept.values("resend_of_id"))):
        count, _ = batch.delete()
        deleted += count
    return deleted
