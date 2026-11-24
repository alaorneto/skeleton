"""Organization identifier availability while a person types (#765).

Each page that asks for a slug has its own read-only route, guarded like the page itself
(signup, platform, organization settings), so the check never opens to anyone the page would
refuse. The answer only says whether the slug is usable; the save checks again in the
transaction. ``slug_check_attrs`` marks the form input that ``site.js`` watches.
"""

from __future__ import annotations

from typing import Any

from django.http import JsonResponse
from django.utils.translation import gettext

# Per person, a brake on enumeration: typing sends one request per pause, never per key.
RATE_LIMIT = 60
RATE_WINDOW_S = 60
MAX_LENGTH = 100


def slug_check_attrs(field: Any, *, url: str, current: str = "") -> None:
    """Point ``field``'s input at the availability route ``url``; ``current`` is the slug being edited."""
    attrs = field.widget.attrs
    attrs["data-slug-check"] = url
    attrs["data-slug-checking"] = gettext("Verificando…")
    attrs["autocomplete"] = "off"
    attrs["spellcheck"] = "false"
    attrs["autocapitalize"] = "none"
    if current:
        attrs["data-slug-current"] = current


def slug_check_response(request: Any, *, organization_id: Any = None) -> JsonResponse:
    """``{"status", "message"}`` for ``?slug=``; ``current`` when it is ``organization_id``'s own slug."""
    from skeleton.common.ratelimit import hit
    from skeleton.tenancy.slugs import availability_message, slug_availability

    if not hit(f"slug-check:{request.user.pk}", limit=RATE_LIMIT, window_s=RATE_WINDOW_S).allowed:
        response = JsonResponse(
            {"status": "rate_limited", "message": gettext("Muitas consultas. Aguarde.")}, status=429
        )
    else:
        slug = request.GET.get("slug", "")
        status = "format" if len(slug) > MAX_LENGTH else slug_availability(slug, organization_id=organization_id)
        response = JsonResponse({"status": status, "message": availability_message(status)})
    response["Cache-Control"] = "no-store"
    return response
