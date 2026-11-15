"""Setup mode routes every request to ``/setup/`` (health probes excepted).

Once the setup is completed and the process has not restarted yet, every request outside the
health probes and the static files gets the waiting page (503, ``Retry-After``): no setup view
runs anymore, so nothing can change the stored configuration (#467).
"""

from django.conf import settings
from django.shortcuts import redirect
from django.template.response import TemplateResponse

ALLOWED_PREFIXES = ("/setup/", "/health/", "/static/")
ALWAYS_SERVED = ("/health/", "/static/")
RETRY_AFTER = 5


def setup_completed() -> bool:
    from skeleton.instance.writer import open_store

    from .mode import setup_completed as completed

    try:
        return completed(open_store(settings.BASE_DIR)[0])
    except Exception:  # noqa: BLE001 - an unreadable store keeps the setup routes, which report it
        return False


class SetupOnlyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.path.startswith(ALWAYS_SERVED) and setup_completed():
            response = TemplateResponse(request, "skeleton/setup/restarting.html", {"title": ""}, status=503)
            response["Retry-After"] = str(RETRY_AFTER)
            return response.render()
        if not request.path.startswith(ALLOWED_PREFIXES):
            return redirect("/setup/")
        return self.get_response(request)
