"""Signed webhook delivery with retries and automatic disablement (#64).

1. The ``webhooks.fanout`` consumer (every event, ``system_context`` of the organization)
   creates one ``WebhookDelivery`` per enabled endpoint subscribed to a webhook event type,
   with a fixed identifiers-only body, and enqueues ``webhooks.deliver`` as the endpoint's
   principal (``run_as="actor"``).
2. ``webhooks.deliver`` re-validates everything as that principal: the endpoint is still
   enabled, the principal can read the record (``unauthorized`` otherwise, nothing is sent)
   and the URL is still a public HTTPS destination. It signs the body and sends it through
   the SSRF-safe client.
3. 2xx succeeds. 5xx, 408, 429, timeouts and network failures are retried after
   1 min, 5 min, 30 min, 2 h, 6 h, 12 h and 24 h; any other answer fails at once. Each failed
   attempt counts in ``WebhookEndpointHealth``; 15 in a row disable the endpoint and notify
   its administrators.

The signature header is ``Skeleton-Webhook-Signature: t=<unix>,v1=<hex>``: an HMAC-SHA256 of
``<t>.<body>`` with each active secret, so a rotation sends two ``v1`` values.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import timedelta
from typing import Any

from django.core.exceptions import PermissionDenied
from django.utils import timezone
from django.utils.translation import gettext

from skeleton.jobs.delivery import RetrySchedule

FANOUT_CONSUMER = "webhooks.fanout"
DELIVER_JOB = "webhooks.deliver"
DISABLED_NOTIFICATION = "webhooks.endpoint_disabled"
RETRY_SCHEDULE = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=30),
    timedelta(hours=2),
    timedelta(hours=6),
    timedelta(hours=12),
    timedelta(hours=24),
)
RETRYABLE_STATUS = frozenset({408, 429})
MAX_CONSECUTIVE_FAILURES = 15
SNIPPET_BYTES = 1024
SIGNATURE_HEADER = "Skeleton-Webhook-Signature"


def sign(body: bytes, secrets: list[str], timestamp: int) -> str:
    """``t=<unix>,v1=<hex>[,v1=<hex>]``: HMAC-SHA256 of ``<t>.<body>`` with each secret, current first."""
    signed = str(timestamp).encode() + b"." + body
    values = [hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest() for secret in secrets]
    return ",".join([f"t={timestamp}", *(f"v1={value}" for value in values)])


def retry_delay(attempt: int) -> timedelta | None:
    """Delay before attempt ``attempt + 1``; ``None`` once the schedule is exhausted."""
    return RetrySchedule(RETRY_SCHEDULE).delay_after(attempt)  # Read at call time.


def retryable(status: int | None) -> bool:
    """``None`` stands for a timeout or a network failure."""
    return status is None or status >= 500 or status in RETRYABLE_STATUS


def sanitized_snippet(body: bytes) -> str:
    """At most 1 KB of the receiver's answer as printable text, never interpreted as HTML."""
    text = body[:SNIPPET_BYTES].decode("utf-8", errors="replace")
    return "".join(character if character.isprintable() or character in "\n\t" else "�" for character in text)


def body_for(event: Any) -> dict[str, Any]:
    return {
        "id": str(event.event_id),
        "type": event.event_type,
        "version": event.version,
        "occurred_at": event.occurred_at.isoformat(),
        "organization": str(event.organization_id),
        "data": {"table": event.table_key, "record": str(event.record_sysid)},
    }


def enqueue_attempt(delivery: Any, principal_id: Any, *, run_after: Any = None) -> None:
    from skeleton.jobs.delivery import enqueue_delivery_attempt

    enqueue_delivery_attempt(
        DELIVER_JOB,
        delivery,
        # The principal is part of the key: a change of principal enqueues a new job (#313).
        idempotency_key=f"webhook:{delivery.pk}:{delivery.attempts + 1}:{principal_id}",
        actor=principal_id,
        run_after=run_after,
    )


def fanout(envelope: Any) -> None:
    """Consumer of every event: one delivery and one job per subscribed, enabled endpoint (idempotent)."""
    from skeleton.events.registry import get_event_type
    from skeleton.tenancy.models import Membership

    from .models import WebhookDelivery, WebhookEndpoint
    from .services import PRINCIPAL_DEACTIVATED, disable_endpoint

    try:
        if not get_event_type(envelope.event_type).webhook:
            return
    except KeyError:
        return
    body = body_for(envelope)
    # One query per event: an endpoint whose principal can no longer act is turned off, not fed
    # deliveries that its job could never run (#313).
    active_principals = set(
        Membership.objects.filter(is_active=True, user__is_active=True, user__account_type="service").values_list(
            "user_id", flat=True
        )
    )
    for endpoint in WebhookEndpoint.objects.filter(is_enabled=True).order_by("sysid"):
        if envelope.event_type not in endpoint.event_types:
            continue
        if endpoint.principal_id not in active_principals:
            locked = WebhookEndpoint.objects.select_for_update().filter(pk=endpoint.pk, is_enabled=True).first()
            if locked is not None:
                disable_endpoint(locked, reason=PRINCIPAL_DEACTIVATED)
            continue
        delivery = WebhookDelivery.objects.filter(
            endpoint=endpoint, event_id=envelope.event_id, resend_of__isnull=True
        ).first()
        if delivery is None:
            delivery = WebhookDelivery(
                organization_id=envelope.organization_id,
                endpoint=endpoint,
                event_id=envelope.event_id,
                event_type=envelope.event_type,
                table_key=envelope.table_key,
                record_sysid=envelope.record_sysid,
                payload=body,
            )
            delivery.save()
            enqueue_attempt(delivery, endpoint.principal_id)


def _health(endpoint: Any) -> Any:
    from .models import WebhookEndpointHealth

    health = WebhookEndpointHealth.objects.select_for_update().filter(endpoint=endpoint).first()
    if health is None:
        health = WebhookEndpointHealth(organization_id=endpoint.organization_id, endpoint=endpoint)
        health.save()
    return health


def _record_attempt(endpoint_id: Any, *, ok: bool, now: Any) -> bool:
    """Count the attempt on the endpoint's health; returns whether the endpoint is still enabled.

    Runs after the HTTP request: the endpoint row is locked only here, for the short write (#295).
    The lock also serializes the creation of the health row between two deliveries of one endpoint.
    """
    from .models import WebhookEndpoint

    endpoint = WebhookEndpoint.objects.select_for_update().filter(pk=endpoint_id).first()
    if endpoint is None:
        return False  # Removed while the request was in flight: nothing to count.
    health = _health(endpoint)
    if ok:
        health.consecutive_failures = 0
        health.last_success_at = now
        health.save(update_fields={"consecutive_failures", "last_success_at"})
        return bool(endpoint.is_enabled)
    health.consecutive_failures += 1
    health.last_failure_at = now
    health.save(update_fields={"consecutive_failures", "last_failure_at"})
    if health.consecutive_failures >= MAX_CONSECUTIVE_FAILURES and endpoint.is_enabled:
        from .services import CONSECUTIVE_FAILURES, disable_endpoint

        disable_endpoint(endpoint, reason=CONSECUTIVE_FAILURES, now=now, failures=MAX_CONSECUTIVE_FAILURES)
    return bool(endpoint.is_enabled)


def _finish(delivery: Any, state: str, *, now: Any, **values: Any) -> None:
    delivery.state = state
    delivery.next_attempt_at = None
    for name, value in values.items():
        setattr(delivery, name, value)
    delivery.save(update_fields={"state", "next_attempt_at", *values})


def deliver(*, delivery_id: str, actor: Any) -> None:
    """Job handler, as the endpoint's principal in the delivery's tenant; never raises for HTTP outcomes."""
    from skeleton.access.policies import authorized_queryset
    from skeleton.common.db import bound_lock_waits
    from skeleton.common.outbound_http import OutboundRequestFailed, OutboundRequestRefused, request
    from skeleton.system.registry import get_spec
    from skeleton.tenancy.context import current_context

    from .models import WebhookDelivery, WebhookEndpoint
    from .services import signing_secrets

    now = timezone.now()
    bound_lock_waits(using=current_context().using)  # A held row fails the job in 5 s, it never hangs it.
    delivery = WebhookDelivery.objects.select_for_update().filter(pk=delivery_id).first()
    if delivery is None or delivery.state not in (WebhookDelivery.State.PENDING, WebhookDelivery.State.RETRYING):
        return  # Retention removed it, or an earlier run already decided it.
    # Read without a lock: only the delivery row stays locked while the request is in flight (#295).
    endpoint = WebhookEndpoint.objects.filter(pk=delivery.endpoint_id).first()
    if endpoint is None or not endpoint.is_enabled:
        _finish(delivery, WebhookDelivery.State.SKIPPED, now=now, error_code="endpoint_unavailable")
        return
    if endpoint.principal_id != actor.pk:
        return  # The endpoint changed principal: the job enqueued for the new one delivers it (#313).
    if delivery.record_sysid is not None:
        try:
            model = get_spec(delivery.table_key).model
            readable = authorized_queryset(model, user=actor, action="read").filter(pk=delivery.record_sysid).exists()
        except (KeyError, LookupError, PermissionDenied):  # Unknown table or denied policy: not readable.
            readable = False
        if not readable:
            _finish(delivery, WebhookDelivery.State.UNAUTHORIZED, now=now, error_code="record_unreadable")
            return
    body = json.dumps(delivery.payload, separators=(",", ":"), sort_keys=True).encode()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Skeleton-Webhooks/1",
        "Skeleton-Webhook-Id": str(delivery.pk),
        "Skeleton-Webhook-Event": delivery.event_type,
        SIGNATURE_HEADER: sign(body, signing_secrets(endpoint, now=now), int(now.timestamp())),
    }
    delivery.attempts += 1
    delivery.last_attempt_at = now
    status, snippet, error = None, "", ""
    try:
        response = request("POST", endpoint.url, headers=headers, body=body)
        status, snippet = response.status, sanitized_snippet(response.body)
    except OutboundRequestRefused as exc:
        error = exc.code  # The URL now points somewhere refused: never retried.
    except OutboundRequestFailed:
        error = "network_error"
    common = {"attempts": delivery.attempts, "last_attempt_at": now, "status_code": status, "response_snippet": snippet}
    if status is not None and 200 <= status < 300:
        _finish(delivery, WebhookDelivery.State.SUCCEEDED, now=now, delivered_at=now, error_code="", **common)
        _record_attempt(endpoint.pk, ok=True, now=now)
        return
    still_enabled = _record_attempt(endpoint.pk, ok=False, now=now)
    refused = bool(error) and error != "network_error"
    delay = None if refused else retry_delay(delivery.attempts)
    if delay is not None and retryable(status) and still_enabled:
        delivery.state = WebhookDelivery.State.RETRYING
        delivery.next_attempt_at = now + delay
        delivery.error_code = error or f"http_{status}"
        delivery.save(update_fields={"state", "next_attempt_at", "error_code", *common})
        enqueue_attempt(delivery, actor.pk, run_after=delivery.next_attempt_at)
        return
    _finish(delivery, WebhookDelivery.State.FAILED, now=now, error_code=error or f"http_{status}", **common)


def register_webhook_delivery() -> None:
    """Job, consumer and notification type of the delivery pipeline; called by the app's ``ready``."""
    from skeleton.events.registry import WILDCARD, register_consumer
    from skeleton.jobs.registry import JobType, register_job_type
    from skeleton.notifications.registry import NotificationType, register_notification_type

    register_job_type(
        JobType(
            DELIVER_JOB,
            deliver,
            {"delivery_id": "uuid"},
            run_as="actor",
            # Crashes only: HTTP outcomes never raise and follow RETRY_SCHEDULE through new jobs.
            max_attempts=3,
            backoff=60.0,
            timeout_s=60,
            owner_app="sk_integrations",
            channel="webhook",
        )
    )
    # Only webhook types: membership and app events, for instance, never reach the fan-out (#43).
    register_consumer(WILDCARD, FANOUT_CONSUMER, fanout, accepts=lambda event_type: event_type.webhook)
    from datetime import timedelta as every

    from skeleton.jobs.periodic import register_periodic

    from .log import RETENTION_JOB, purge_expired_deliveries

    register_job_type(
        JobType(
            RETENTION_JOB, purge_expired_deliveries, {}, run_as="system", max_attempts=3, owner_app="sk_integrations"
        )
    )
    register_periodic(RETENTION_JOB, RETENTION_JOB, every=every(hours=6))
    register_notification_type(
        NotificationType(
            DISABLED_NOTIFICATION,
            gettext("Webhook desativado"),
            payload_fields=("endpoint", "failures", "reason"),
            owner_app="sk_integrations",
        )
    )
