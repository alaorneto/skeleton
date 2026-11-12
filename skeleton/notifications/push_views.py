"""Web push opt-in: subscribe and unsubscribe (session and CSRF) and the service worker (#56)."""

from django.db import transaction
from django.http import Http404, HttpResponse
from django.utils.translation import gettext
from django.views.decorators.cache import cache_control
from django.views.decorators.http import require_safe
from rest_framework.response import Response

from skeleton.api.errors import BoundaryError
from skeleton.api.openapi import UUID, Operation, obj, string
from skeleton.api.views import SessionAPI, request_fields

from .channels import push

SERVICE_WORKER = """'use strict';
self.addEventListener('push', function (event) {
  var data = {};
  try { data = event.data ? event.data.json() : {}; } catch (error) { data = {}; }
  var title = typeof data.title === 'string' ? data.title : 'Notificação';
  var options = {body: typeof data.body === 'string' ? data.body : '', data: {url: data.url}};
  event.waitUntil(self.registration.showNotification(title, options));
});
self.addEventListener('notificationclick', function (event) {
  event.notification.close();
  var url = event.notification.data && event.notification.data.url;
  if (typeof url === 'string' && new URL(url, self.location.origin).origin === self.location.origin) {
    event.waitUntil(self.clients.openWindow(url));
  }
});
"""


class PushSubscriptionsAPI(SessionAPI):
    """``POST {"endpoint", "keys": {"p256dh", "auth"}}`` registers this browser for the signed-in person."""

    openapi = {
        "POST": Operation(
            "Registra este navegador para web push",
            obj({"sysid": UUID}),
            status=201,
            request=obj(
                {
                    "endpoint": string(),
                    "keys": obj({"p256dh": string(), "auth": string()}),
                    "expirationTime": {},
                },
                required=("endpoint", "keys"),
            ),
            description="404 quando o push não está configurado na instância.",
        )
    }

    @transaction.atomic
    def post(self, request):
        if not push.push_configured():
            raise Http404
        data = request_fields(request)
        keys = data.get("keys")
        extra = set(data) - {"endpoint", "keys", "expirationTime"}
        if extra or not isinstance(keys, dict) or set(keys) - {"p256dh", "auth"}:
            raise BoundaryError("invalid_request", gettext("Envie endpoint e keys (p256dh, auth)."))
        subscription = push.subscribe(
            request.user,
            endpoint=data.get("endpoint"),
            p256dh=keys.get("p256dh"),
            auth=keys.get("auth"),
            user_agent=request.headers.get("User-Agent", ""),
        )
        return Response({"sysid": subscription.sysid}, status=201)


class PushUnsubscribeAPI(SessionAPI):
    openapi = {
        "POST": Operation("Cancela o web push deste navegador", None, status=204, request=obj({"endpoint": string()}))
    }

    @transaction.atomic
    def post(self, request):
        data = request_fields(request)
        if set(data) - {"endpoint"}:
            raise BoundaryError("invalid_request", gettext("Envie só o endpoint."))
        push.unsubscribe(request.user, endpoint=data.get("endpoint"))
        return Response(status=204)


@require_safe
@cache_control(no_cache=True)
def service_worker(request):
    if not push.push_configured():
        raise Http404
    response = HttpResponse(SERVICE_WORKER, content_type="application/javascript; charset=utf-8")
    response["Service-Worker-Allowed"] = "/"
    return response
