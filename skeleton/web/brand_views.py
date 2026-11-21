"""``/brand/``: the stylesheet of the active theme and the images of the instance (#597).

Public by the platform administrator's choice, like the published brand of an organization
host: no login, GET/HEAD only, no parameter that picks a file. The stylesheet is served as
``text/css`` with ``nosniff`` from the same host (``style-src 'self'`` already allows it), never
embedded in HTML, so the CSP does not change. ``?safe=1`` serves only the generated tokens,
without the free CSS: the appearance pages use it, so a theme that hides the interface never
locks the administrator out. The images come from the three keys of the single brand row or,
when the administrator did not upload one, from the active theme package (#636), whose fonts
are served by ``/brand/fonts/<file>`` only when the file is a font of the active theme. An SVG
logo goes out with ``image/svg+xml``, ``nosniff`` and a ``sandbox`` CSP.
"""

from __future__ import annotations

import re

from django.http import Http404, HttpResponse, HttpResponseNotModified
from django.views.decorators.http import require_safe

from skeleton.web.organization_settings_views import public_site_image

__all__ = [
    "brand_font",
    "brand_icon",
    "brand_logo",
    "brand_logo_dark",
    "brand_manifest",
    "brand_theme",
    "stylesheet_response",
]

IMMUTABLE = "public, max-age=31536000, immutable"
SHORT = "public, max-age=300"
SVG_POLICY = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
FONT_NAME = re.compile(r"[a-z0-9][a-z0-9._-]*\.woff2")


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


def asset_response(request, content: bytes | None, mime: str, version: str, *, cache_control: str | None = None):
    """Bytes of a theme file (#636): ``nosniff``, sandbox CSP for SVG, immutable with the right ``?v=``."""
    if content is None:
        raise Http404
    response = HttpResponse(content, content_type=mime)
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Disposition"] = "inline"
    response["Content-Security-Policy"] = SVG_POLICY if mime == "image/svg+xml" else "default-src 'none'; sandbox"
    if cache_control is None:
        cache_control = IMMUTABLE if version and request.GET.get("v") == version else SHORT
    response["Cache-Control"] = cache_control
    response["ETag"] = f'"{version}"'
    return response


def _image(request, kind: str):
    from skeleton.appearance.assets import open_asset
    from skeleton.appearance.images import open_image
    from skeleton.appearance.snapshot import current_appearance

    appearance = current_appearance()
    source = appearance.icon_source if kind == "icon" else appearance.logo_source
    if source == "theme":
        ref = appearance.theme_asset(kind)
        if ref is None or (kind == "icon" and ref.mime != "image/png"):
            raise Http404
        return asset_response(request, open_asset(ref), ref.mime, ref.version, cache_control=SHORT)
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


@require_safe
def brand_font(request, name):
    """A font of the active theme package; any other name is 404 (no parameter picks a file)."""
    from skeleton.appearance.assets import open_asset
    from skeleton.appearance.snapshot import current_appearance

    if not FONT_NAME.fullmatch(name):
        raise Http404
    ref = current_appearance().theme_asset(name)
    if ref is None or ref.mime != "font/woff2":
        raise Http404
    return asset_response(request, open_asset(ref), ref.mime, ref.version)


@require_safe
def brand_manifest(request):
    """``/brand/manifest.webmanifest`` (#639): name, colour and icon of the instance, only on the main domain."""
    from django.http import JsonResponse
    from django.templatetags.static import static

    from skeleton.appearance.snapshot import current_appearance

    if getattr(request, "organization_site", None) is not None:
        raise Http404  # On an organization host the manifest is /site/manifest.webmanifest.
    appearance = current_appearance()
    name = appearance.product_name or "Skeleton"
    manifest: dict[str, object] = {"name": name, "short_name": name[:12], "start_url": "/", "display": "standalone"}
    if appearance.brand_color:
        manifest["theme_color"] = manifest["background_color"] = appearance.brand_color
    if appearance.icon_version:
        icon = {"src": f"/brand/icon/?v={appearance.icon_version}", "sizes": "512x512", "type": "image/png"}
        manifest["icons"] = [{**icon, "purpose": "any"}]
    else:
        manifest["icons"] = [{"src": static("skeleton/web/favicon.svg"), "sizes": "any", "type": "image/svg+xml"}]
    response = JsonResponse(manifest, content_type="application/manifest+json")
    response["Cache-Control"] = SHORT
    response["X-Content-Type-Options"] = "nosniff"
    return response
