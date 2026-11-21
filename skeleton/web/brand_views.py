"""``/brand/``: the stylesheet of the active theme and the images of the instance (#597).

Public by the platform administrator's choice, like the published brand of an organization
host: no login, GET/HEAD only, no parameter that picks a file. The stylesheet is served as
``text/css`` with ``nosniff`` from the same host (``style-src 'self'`` already allows it), never
embedded in HTML, so the CSP does not change. ``?safe=1`` serves only the generated tokens,
without the free CSS: the appearance pages use it, so a theme that hides the interface never
locks the administrator out. The images come only from the three keys of the single brand row.
"""

from __future__ import annotations

from django.http import HttpResponse, HttpResponseNotModified
from django.views.decorators.http import require_safe

from skeleton.web.organization_settings_views import public_site_image

__all__ = ["brand_icon", "brand_logo", "brand_logo_dark", "brand_theme", "stylesheet_response"]

IMMUTABLE = "public, max-age=31536000, immutable"
SHORT = "public, max-age=300"


def stylesheet_response(request, css: str, version: str, *, cache_control: str | None = None) -> HttpResponse:
    """A theme stylesheet with ETag, ``nosniff`` and the cache policy of its version."""
    etag = f'"{version or "core"}"'
    if cache_control is None:
        cache_control = IMMUTABLE if version and request.GET.get("v") == version else SHORT
    if etag in [item.strip() for item in request.headers.get("If-None-Match", "").split(",")]:
        response = HttpResponseNotModified()
    else:
        response = HttpResponse(css, content_type="text/css; charset=utf-8")
    response["ETag"] = etag
    response["Cache-Control"] = cache_control
    response["X-Content-Type-Options"] = "nosniff"
    return response


@require_safe
def brand_theme(request):
    """The CSS of the active theme; 200 with an empty body for the core theme (the link never 404s)."""
    from skeleton.appearance.snapshot import current_appearance

    appearance = current_appearance()
    if request.GET.get("safe") == "1":
        return stylesheet_response(request, appearance.theme_css_tokens, appearance.theme_safe_version)
    return stylesheet_response(request, appearance.theme_css, appearance.theme_version)


def _image(request, kind: str):
    from skeleton.appearance.images import open_image

    return public_site_image(request, open_image(kind))


@require_safe
def brand_logo(request):
    return _image(request, "logo")


@require_safe
def brand_logo_dark(request):
    return _image(request, "logo_dark")


@require_safe
def brand_icon(request):
    return _image(request, "icon")
