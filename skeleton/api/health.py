import logging
from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.http import HttpResponseNotAllowed, JsonResponse

from skeleton.common.logging import security_event


def _cache_works() -> bool:
    key = "skeleton-health-" + str(uuid4())
    try:
        cache.set(key, "ok", timeout=10)
        return cache.get(key) == "ok"
    except Exception:  # noqa: BLE001 - any cache failure is the same "degraded"
        return False
    finally:
        try:
            cache.delete(key)
        except Exception:  # noqa: BLE001, S110 - the cache is already reported as degraded
            pass


def probe_response(request):
    request.get_host()  # Health never bypasses ALLOWED_HOSTS.
    if request.method not in {"GET", "HEAD"}:
        return HttpResponseNotAllowed(["GET", "HEAD"])
    ready = request.path_info == "/health/ready/"
    body: dict[str, object] = {"status": "ok"}
    drain = getattr(settings, "SKELETON_DRAIN_MARKER", None)
    if ready and drain and Path(drain).exists():
        # A rolling update (#682) takes this node out of the balancer before stopping it.
        body = {"status": "draining"}
    elif ready:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                if cursor.fetchone() != (1,):
                    raise RuntimeError("Database probe failed.")
        except Exception:  # noqa: BLE001 - a failing database answers 503 without detail
            body = {"status": "unavailable"}
            # The probe answers 503 without detail; operators get the failing dependency and code position.
            security_event(
                "health.probe_failed",
                level=logging.WARNING,
                status=503,
                outcome="server_error",
                reason_code="database_unavailable",
                source="health",
                exc_info=True,
            )
        else:
            # The cache degrades, never fails, the readiness (#675): every replica shares the same
            # Redis, so failing them would take the whole site down while logged-in pages still work.
            if not _cache_works():
                body["degraded"] = ["cache"]
                security_event(
                    "health.probe_degraded",
                    level=logging.WARNING,
                    status=200,
                    outcome="degraded",
                    reason_code="cache_unavailable",
                    source="health",
                )
    response = JsonResponse(body, status=200 if body["status"] == "ok" else 503)
    response["Cache-Control"] = "no-store"
    if request.method == "HEAD":
        response.content = b""
    return response


class HealthProbeMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path_info in {"/health/live/", "/health/ready/"}:
            return probe_response(request)
        return self.get_response(request)
