"""The single 503 answer of both maintenance modes (#446): HTML for browsers, the JSON envelope for the API.

It runs before sessions, authentication and the database: the template is rendered without a
request, so no context processor runs, and nothing here touches the ORM. The language comes
from ``Accept-Language`` (the ``LocaleMiddleware`` runs later).
"""

from __future__ import annotations

from datetime import UTC, datetime

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.template.loader import render_to_string
from django.utils import translation
from django.utils.translation import gettext

ADMINISTRATIVE_DEFAULT = "O sistema está em manutenção. Tente novamente mais tarde."
DEPLOYMENT_DEFAULT = "O sistema está sendo atualizado. Tente novamente em instantes."


def platform_login_url() -> str:
    """The login that brings a platform administrator back to the maintenance page (#447)."""
    from urllib.parse import urlencode

    from django.urls import reverse

    return f"{reverse('account_login')}?{urlencode({'next': reverse('platform:maintenance')})}"


def wants_json(request: HttpRequest) -> bool:
    """The API prefix, or a client that asks for JSON and not for HTML."""
    if request.path_info.startswith("/api/"):
        return True
    accept = request.headers.get("Accept", "")
    return "application/json" in accept and "text/html" not in accept


def maintenance_response(
    request: HttpRequest,
    *,
    message: str | None,
    ends_at: datetime | None,
    retry_after: int,
    deployment: bool = False,
    login_url: str | None = None,
) -> HttpResponse:
    request.get_host()  # An unknown Host is refused (400) before anything is rendered.
    language = translation.get_language_from_request(request)
    with translation.override(language):
        detail = message or gettext(DEPLOYMENT_DEFAULT if deployment else ADMINISTRATIVE_DEFAULT)
        end = ends_at.astimezone(UTC) if ends_at else None
        response: HttpResponse
        if wants_json(request):
            # The API error envelope (docs/api.md) plus the scheduled end, null without one.
            payload = {"code": "maintenance", "detail": detail, "ends_at": end.isoformat() if end else None}
            response = JsonResponse(payload, status=503)
        else:
            html = render_to_string(
                "skeleton/maintenance.html",
                {
                    "language": language,
                    "detail": detail,
                    "ends_at": end,
                    "ends_at_iso": end.isoformat() if end else "",
                    "login_url": login_url,
                },
            )
            response = HttpResponse(html, status=503, content_type="text/html; charset=utf-8")
    response["Cache-Control"] = "no-store"
    response["Retry-After"] = str(retry_after)
    if request.method == "HEAD":
        response.content = b""
    # Metrics label these answers as "maintenance", never by the path (skeleton.observability.metrics).
    request.skeleton_maintenance_response = True  # type: ignore[attr-defined]
    return response
