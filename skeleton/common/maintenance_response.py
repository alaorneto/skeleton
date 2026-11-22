"""The 503 answer of the deployment gate: HTML for browsers, the JSON envelope for the API.

The deployment agent closes the instance with its marker during an update (#446; the
administrative mode left the application in #674). The answer runs before sessions,
authentication and the database: the template is rendered without a request, so no context
processor runs, and nothing here touches the ORM. The language comes from ``Accept-Language``
(the ``LocaleMiddleware`` runs later).
"""

from __future__ import annotations

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.template.loader import render_to_string
from django.utils import translation
from django.utils.translation import gettext

DEPLOYMENT_DEFAULT = "O sistema está sendo atualizado. Tente novamente em instantes."
RETRY_AFTER_S = 60


def wants_json(request: HttpRequest) -> bool:
    """The API prefix, or a client that asks for JSON and not for HTML."""
    if request.path_info.startswith("/api/"):
        return True
    accept = request.headers.get("Accept", "")
    return "application/json" in accept and "text/html" not in accept


def maintenance_response(request: HttpRequest) -> HttpResponse:
    request.get_host()  # An unknown Host is refused (400) before anything is rendered.
    language = translation.get_language_from_request(request)
    with translation.override(language):
        detail = gettext(DEPLOYMENT_DEFAULT)
        response: HttpResponse
        if wants_json(request):
            # The API error envelope (docs/api.md); ends_at stays null: an update has no scheduled end.
            response = JsonResponse({"code": "maintenance", "detail": detail, "ends_at": None}, status=503)
        else:
            html = render_to_string("skeleton/maintenance.html", {"language": language, "detail": detail})
            response = HttpResponse(html, status=503, content_type="text/html; charset=utf-8")
    response["Cache-Control"] = "no-store"
    response["Retry-After"] = str(RETRY_AFTER_S)
    if request.method == "HEAD":
        response.content = b""
    # Metrics label these answers as "maintenance", never by the path (skeleton.observability.metrics).
    request.skeleton_maintenance_response = True  # type: ignore[attr-defined]
    return response
