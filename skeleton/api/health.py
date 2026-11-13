import logging
from uuid import uuid4

from django.core.cache import cache
from django.db import connection
from django.http import HttpResponseNotAllowed, JsonResponse

from skeleton.common.logging import security_event


def probe_response(request):
    request.get_host()  # Health never bypasses ALLOWED_HOSTS.
    if request.method not in {"GET", "HEAD"}:
        return HttpResponseNotAllowed(["GET", "HEAD"])
    ready = request.path_info == "/health/ready/"
    status = "ok"
    if ready:
        dependency = "database"
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                if cursor.fetchone() != (1,):
                    raise RuntimeError("Database probe failed.")
            dependency = "cache"
            key = "skeleton-health-" + str(uuid4())
            try:
                cache.set(key, "ok", timeout=10)
                if cache.get(key) != "ok":
                    raise RuntimeError("Cache probe failed.")
            finally:
                cache.delete(key)
        except Exception:  # noqa: BLE001 - any failing dependency answers 503 without detail
            status = "unavailable"
            # The probe answers 503 without detail; operators get the failing dependency and code position.
            security_event(
                "health.probe_failed",
                level=logging.WARNING,
                status=503,
                outcome="server_error",
                reason_code=dependency + "_unavailable",
                source="health",
                exc_info=True,
            )
    response = JsonResponse({"status": status}, status=200 if status == "ok" else 503)
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
