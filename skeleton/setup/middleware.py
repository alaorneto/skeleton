"""Setup mode routes every request to ``/setup/`` (health probes excepted).

Once the setup is completed and the process has not restarted yet, every request outside the
health probes and the static files gets the waiting page (503, ``Retry-After``): no setup view
runs anymore, so nothing can change the stored configuration (#467).
"""

import sqlite3

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import redirect
from django.template.response import TemplateResponse

from skeleton.instance.store import InstanceStoreError

ALLOWED_PREFIXES = ("/setup/", "/health/", "/static/")
ALWAYS_SERVED = ("/health/", "/static/")
RETRY_AFTER = 5


def setup_completed() -> bool:
    from skeleton.instance.writer import open_store

    from .mode import setup_completed as completed

    return completed(open_store(settings.BASE_DIR)[0])


class SetupOnlyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.path.startswith(ALWAYS_SERVED):
            try:
                done = setup_completed()
            except (sqlite3.Error, InstanceStoreError):
                response = HttpResponse(
                    "Armazém da instância temporariamente indisponível. Tente novamente.",
                    status=503,
                    content_type="text/plain; charset=utf-8",
                )
                response["Retry-After"] = str(RETRY_AFTER)
                return response
            if done:
                response = TemplateResponse(request, "skeleton/setup/restarting.html", {"title": ""}, status=503)
                response["Retry-After"] = str(RETRY_AFTER)
                return response.render()
        if not request.path.startswith(ALLOWED_PREFIXES):
            return redirect("/setup/")
        return self.get_response(request)
