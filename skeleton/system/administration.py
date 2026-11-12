"""Application administration is derived from the active, code-owned registry."""

from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.urls import resolve, reverse

from skeleton.access.capabilities import APPS_MANAGE, require_capability
from skeleton.tenancy.context import current_context

from .app_registry import get_app
from .applications import require_application_enabled


def application_manifest(app_label):
    try:
        return get_app(app_label)
    except KeyError:
        raise Http404 from None


def application_identity(manifest):
    """Never derive release identity from editable catalog descriptions."""
    return {
        "app_id": manifest.app_id or manifest.app_label,
        "app_label": manifest.app_label,
        "label": manifest.label,
        "description": manifest.description,
        "version": manifest.version,
        "origin": manifest.origin,
        "compatibility": (manifest.canonical or {}).get("compatibility", {}),
        "dependencies": manifest.dependencies,
    }


def resolve_application_screen(app_label, screen_id, *, user, scope):
    """Shared policy for discovery, dispatch, and app-owned direct URLs."""
    manifest = application_manifest(app_label)
    screen = next((item for item in manifest.administration.screens if item.id == screen_id), None)
    if screen is None or screen.scope != scope:
        raise Http404
    if scope == "platform":
        from .releases import require_platform

        require_platform(user)
    else:
        # Tenant parameter screens (organization_admin_only in the manifest) need apps.manage (#24).
        require_capability(user, APPS_MANAGE)
    return manifest, screen


def screen_sections(manifest, *, user, scope):
    entries = {}
    for screen in sorted(manifest.administration.screens, key=lambda item: (item.order, item.id)):
        if screen.scope != scope:
            continue
        try:
            resolve_application_screen(manifest.app_label, screen.id, user=user, scope=scope)
        except PermissionDenied:
            continue
        url = (
            reverse("platform:app-screen", kwargs={"app_label": manifest.app_label, "screen_id": screen.id})
            if scope == "platform"
            else reverse(
                "application-admin-screen",
                kwargs={
                    "organization_id": current_context().organization_id,
                    "app_label": manifest.app_label,
                    "screen_id": screen.id,
                },
            )
        )
        entries.setdefault(screen.section_id, []).append({"id": screen.id, "label": screen.label, "url": url})
    return [
        {"id": section.id, "label": section.label, "screens": entries[section.id]}
        for section in sorted(manifest.administration.sections, key=lambda item: (item.order, item.id))
        if section.id in entries
    ]


def guard_application_screen(request, view_name, *, app_label=None):
    """Parameter screens require tenant administration; other app routes require activation."""
    if app_label is not None:
        manifest = application_manifest(app_label)
    else:
        namespace = view_name.split(":", 1)[0]
        try:
            manifest = get_app(namespace)
        except KeyError:
            return
    for screen in manifest.administration.screens:
        if screen.scope == "organization" and screen.kind == "custom" and screen.url_name == view_name:
            resolve_application_screen(manifest.app_label, screen.id, user=request.user, scope="organization")
            return
    require_application_enabled(manifest.app_label)


def guard_application_urls(patterns, manifest):
    """Clone the URL tree so module-owned URLPattern objects stay untouched."""
    from functools import wraps

    from django.urls.resolvers import URLPattern, URLResolver

    result = []
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            result.append(
                URLResolver(
                    pattern.pattern,
                    guard_application_urls(pattern.url_patterns, manifest),
                    default_kwargs=pattern.default_kwargs,
                    app_name=pattern.app_name,
                    namespace=pattern.namespace,
                )
            )
        elif isinstance(pattern, URLPattern):

            def protect(callback):
                @wraps(callback)
                def guarded(request, *args, **kwargs):
                    guard_application_screen(request, request.resolver_match.view_name, app_label=manifest.app_label)
                    return callback(request, *args, **kwargs)

                return guarded

            result.append(
                URLPattern(
                    pattern.pattern, protect(pattern.callback), default_args=pattern.default_args, name=pattern.name
                )
            )
        else:
            raise TypeError(f"Unsupported application URL declaration: {manifest.app_label}.")
    return result


def guard_application_api_urls(patterns, manifest):
    """Clone the API URL tree and close every endpoint of a disabled application.

    The gate runs before the view, so an endpoint that never calls ``authorized_queryset``
    is still closed. ``functools.wraps`` keeps ``csrf_exempt`` and the DRF ``cls``/``initkwargs``
    attributes; names and namespaces are copied, so ``reverse`` keeps resolving.
    """
    from functools import wraps

    from django.http import JsonResponse
    from django.urls.resolvers import URLPattern, URLResolver

    result = []
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            result.append(
                URLResolver(
                    pattern.pattern,
                    guard_application_api_urls(pattern.url_patterns, manifest),
                    default_kwargs=pattern.default_kwargs,
                    app_name=pattern.app_name,
                    namespace=pattern.namespace,
                )
            )
        elif isinstance(pattern, URLPattern):

            def protect(callback):
                @wraps(callback)
                def guarded(request, *args, **kwargs):
                    if current_context().organization_id is None:
                        # The tenant middleware opens no tenant context for anonymous sessions.
                        return JsonResponse(
                            {"code": "permission_denied", "detail": "Authentication credentials were not provided."},
                            status=403,
                        )
                    try:
                        get_app(manifest.app_label)
                        require_application_enabled(manifest.app_label)
                    except (KeyError, PermissionDenied):
                        return JsonResponse({"code": "not_found", "detail": "Not found."}, status=404)
                    return callback(request, *args, **kwargs)

                return guarded

            result.append(
                URLPattern(
                    pattern.pattern, protect(pattern.callback), default_args=pattern.default_args, name=pattern.name
                )
            )
        else:
            raise TypeError(f"Unsupported application API URL declaration: {manifest.app_label}.")
    return result


def tenant_screen_url(manifest, screen, organization_id):
    if screen.kind == "configuration":
        return reverse(
            "application-configuration", kwargs={"organization_id": organization_id, "app_label": manifest.app_label}
        )
    if not screen.url_name or screen.url_name.split(":", 1)[0] != manifest.app_label:
        raise Http404
    url = reverse(screen.url_name, kwargs={"organization_id": organization_id})
    prefix = f"/o/{organization_id}/apps/{manifest.app_label}/"
    if not url.startswith(prefix) or url.startswith("//") or resolve(url).view_name != screen.url_name:
        raise Http404
    return url


def guard_user_urls(patterns):
    """Global user routes of a first-party app (``/me/apps/<label>/``, #66): login and no cache.

    They have no organization in the URL, so they cannot check activation here: each view reads
    the person's organizations one transaction at a time (``for_each_organization``) and skips
    those where the app is not active.
    """
    from functools import wraps

    from django.contrib.auth.views import redirect_to_login
    from django.urls.resolvers import URLPattern, URLResolver
    from django.views.decorators.cache import never_cache

    result = []
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            result.append(
                URLResolver(
                    pattern.pattern,
                    guard_user_urls(pattern.url_patterns),
                    default_kwargs=pattern.default_kwargs,
                    app_name=pattern.app_name,
                    namespace=pattern.namespace,
                )
            )
        elif isinstance(pattern, URLPattern):

            def protect(callback):
                @never_cache
                @wraps(callback)
                def guarded(request, *args, **kwargs):
                    # A token route (a calendar feed) authenticates by its own token in the path.
                    if not request.user.is_authenticated and not getattr(callback, "skeleton_token_route", False):
                        return redirect_to_login(request.get_full_path())
                    return callback(request, *args, **kwargs)

                return guarded

            result.append(
                URLPattern(
                    pattern.pattern, protect(pattern.callback), default_args=pattern.default_args, name=pattern.name
                )
            )
        else:
            raise TypeError("Unsupported user URL declaration.")
    return result
