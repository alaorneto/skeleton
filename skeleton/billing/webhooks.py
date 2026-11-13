"""Inbound payment webhooks (#400): verify, record once, enqueue; never trust the body.

``POST /billing/webhooks/<provider>/<account sysid>/`` sits outside ``/o/`` and ``/api/``: the
sender is a provider, not a person, so there is no session, no CSRF token and no tenant. The
view:

1. caps the body (``MAX_BODY``) and the rate per provider account;
2. loads the enabled ``ProviderAccount`` of that provider (an unknown one answers 404);
3. asks the adapter to verify the signature or token and the timestamp, in constant time;
4. records a ``ProviderEvent`` once per ``(account, event id)``: a replay is acknowledged and
   ignored;
5. enqueues ``billing.process_event``, which fetches the payment from the provider with the
   platform's own key before anything changes, and answers 200 at once.

A refused webhook answers 400 and changes nothing (the middleware rolls the request back).
"""

from __future__ import annotations

import hashlib
import logging
from uuid import UUID

from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

logger = logging.getLogger("skeleton.billing")
MAX_BODY = 256 * 1024
RATE_PER_MINUTE = 120


def _headers(request) -> dict[str, str]:
    return {name.lower(): value for name, value in request.headers.items()}


@csrf_exempt
@require_POST
def receive(request, provider: str, account_id: UUID):
    from skeleton.common.ratelimit import hit
    from skeleton.jobs.services import enqueue

    from .models import ProviderAccount, ProviderEvent
    from .payments import PROCESS_JOB
    from .providers.base import WebhookRejected, get_provider

    try:
        length = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = MAX_BODY + 1
    if length > MAX_BODY:
        return JsonResponse({"code": "payload_too_large"}, status=413)
    if not hit(f"billing:webhook:{account_id}", limit=RATE_PER_MINUTE, window_s=60).allowed:
        return JsonResponse({"code": "rate_limited"}, status=429)
    account = ProviderAccount.objects.filter(pk=account_id, provider=provider, enabled=True).first()
    if account is None:
        return JsonResponse({"code": "not_found"}, status=404)
    try:
        adapter = get_provider(provider)
    except KeyError:
        return JsonResponse({"code": "not_found"}, status=404)
    body = request.body[: MAX_BODY + 1]
    if len(body) > MAX_BODY:
        return JsonResponse({"code": "payload_too_large"}, status=413)
    try:
        notification = adapter.verify_webhook(
            account, _headers(request), body, now=timezone.now(), query=request.GET.dict()
        )
    except WebhookRejected as exc:
        logger.warning("billing.webhook_rejected", extra={"provider": provider, "code": exc.code})
        return JsonResponse({"code": "invalid_signature"}, status=400)
    event_id = notification.event_id[:200]
    if ProviderEvent.objects.filter(provider_account=account, event_id=event_id).exists():
        return JsonResponse({"received": True, "duplicate": True})
    record = ProviderEvent(
        provider_account=account,
        event_id=event_id,
        kind=notification.kind[:100],
        payment_reference=(notification.payment_reference or "")[:200],
        body_digest=hashlib.sha256(body).hexdigest(),
    )
    record.save()
    enqueue(PROCESS_JOB, payload={"event": str(record.pk)}, idempotency_key=f"provider-event:{record.pk}")
    return HttpResponse(status=200)
