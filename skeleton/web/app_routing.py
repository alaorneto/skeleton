"""Routing of the URLs apps declare (#338): guards around every view, mounted under the core roots.

The policy (which screen a person may open, whether an app is active) stays in skeleton.system;
this module only wraps the declared URL trees, so skeleton.system never imports a surface.
"""

from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.utils.translation import gettext

from skeleton.system.administration import guard_application_screen
from skeleton.system.app_registry import all_apps, get_app
from skeleton.system.applications import require_application_enabled
from skeleton.tenancy.context import current_context
from skeleton.tenancy.routing import ORG


def _administrative_mfa_missing(user):
    """Same gate as tenant_page and TenantAPI: any capability requires MFA when the policy is on (#322)."""
    from skeleton.access.capabilities import has_any_capability
    from skeleton.accounts.mfa import mfa_required

    if not getattr(user, "is_authenticated", False):
        return False
    return has_any_capability(user) and mfa_required(user)


def guard_application_urls(patterns, manifest):
    """Clone the URL tree so module-owned URLPattern objects stay untouched.

    Before the view (#322): an anonymous visitor goes to the login page, a person without a
    tenant context gets 404, the app must be active (or the screen allowed), and anyone holding
    a capability passes the administrative MFA gate, whether or not the view uses ``tenant_page``.
    """
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
                    if not request.user.is_authenticated:
                        from django.contrib.auth.views import redirect_to_login

                        return redirect_to_login(request.get_full_path())
                    if current_context().organization_id is None:
                        raise Http404
                    guard_application_screen(request, request.resolver_match.view_name, app_label=manifest.app_label)
                    if _administrative_mfa_missing(request.user):
                        from django.shortcuts import redirect

                        return redirect("mfa_index")
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
                    if _administrative_mfa_missing(request.user):  # The answer of TenantAPI (#322).
                        return JsonResponse(
                            {
                                "code": "mfa_required",
                                "detail": gettext("Configure autenticação multifator para continuar."),
                            },
                            status=403,
                        )
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


def _portal_gate(manifest):
    """``None`` when the app's portal may serve this request, else the reason it may not."""
    if not current_context().external or current_context().organization_id is None:
        return "context"
    try:
        get_app(manifest.app_label)
        require_application_enabled(manifest.app_label)  # Active, available and, with billing, contracted.
    except (KeyError, PermissionDenied):
        return "app"
    return None


def guard_portal_urls(patterns, manifest):
    """Clone the portal URL tree of an app (#533): login, no cache, external context, app active."""
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
                    guard_portal_urls(pattern.url_patterns, manifest),
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
                    if not request.user.is_authenticated:
                        return redirect_to_login(request.get_full_path())
                    if _portal_gate(manifest) is not None:
                        raise Http404
                    return callback(request, *args, **kwargs)

                return guarded

            result.append(
                URLPattern(
                    pattern.pattern, protect(pattern.callback), default_args=pattern.default_args, name=pattern.name
                )
            )
        else:
            raise TypeError(f"Unsupported portal URL declaration: {manifest.app_label}.")
    return result


def guard_portal_api_urls(patterns, manifest):
    """Clone the portal API URL tree of an app (#533); a closed app or another context is a 404."""
    from functools import wraps

    from django.http import JsonResponse
    from django.urls.resolvers import URLPattern, URLResolver

    result = []
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            result.append(
                URLResolver(
                    pattern.pattern,
                    guard_portal_api_urls(pattern.url_patterns, manifest),
                    default_kwargs=pattern.default_kwargs,
                    app_name=pattern.app_name,
                    namespace=pattern.namespace,
                )
            )
        elif isinstance(pattern, URLPattern):

            def protect(callback):
                @wraps(callback)
                def guarded(request, *args, **kwargs):
                    if _portal_gate(manifest) is not None:
                        return JsonResponse({"code": "not_found", "detail": "Not found."}, status=404)
                    response = callback(request, *args, **kwargs)
                    response["Cache-Control"] = "no-store"
                    return response

                return guarded

            result.append(
                URLPattern(
                    pattern.pattern, protect(pattern.callback), default_args=pattern.default_args, name=pattern.name
                )
            )
        else:
            raise TypeError(f"Unsupported portal API URL declaration: {manifest.app_label}.")
    return result


def portal_namespace(app_label):
    """The URL namespace of an app's portal pages: ``reverse(f"{portal_namespace(label)}:<name>")``."""
    return f"{app_label}_portal"


def portal_urlpatterns(manifest):
    """The portal routes of one app: ``/portal/<org>/apps/<label>/`` and ``/api/v1/portal/<org>/apps/<label>/``."""
    from django.urls import include, path

    result = []
    if manifest.portal_urls:
        module, _, _ = include(manifest.portal_urls)
        patterns = module.urlpatterns if hasattr(module, "urlpatterns") else module
        label = portal_namespace(manifest.app_label)
        result.append(
            path(
                f"portal/<uuid:organization_id>/apps/{manifest.app_label}/",
                include((guard_portal_urls(patterns, manifest), label), namespace=label),
            )
        )
    if manifest.portal_api_urls:
        module, _, _ = include(manifest.portal_api_urls)
        patterns = module.urlpatterns if hasattr(module, "urlpatterns") else module
        label = portal_namespace(manifest.app_label) + "_api"
        result.append(
            path(
                f"api/v1/portal/<uuid:organization_id>/apps/{manifest.app_label}/",
                include((guard_portal_api_urls(patterns, manifest), label), namespace=label),
            )
        )
    return result


def application_urlpatterns():
    from django.urls import include, path

    result = []
    for manifest in all_apps():
        if manifest.urls:
            module, app_name, namespace = include(manifest.urls)
            patterns = module.urlpatterns if hasattr(module, "urlpatterns") else module
            guarded = guard_application_urls(patterns, manifest)
            result.append(
                path(
                    f"{ORG}apps/{manifest.app_label}/",
                    include((guarded, app_name), namespace=namespace),
                )
            )
        if manifest.bundled and manifest.user_urls:
            module, app_name, namespace = include(manifest.user_urls)
            patterns = module.urlpatterns if hasattr(module, "urlpatterns") else module
            result.append(
                path(
                    f"me/apps/{manifest.app_label}/",
                    include((guard_user_urls(patterns), app_name), namespace=namespace or app_name),
                )
            )
        result.extend(portal_urlpatterns(manifest))
        if manifest.api_urls:
            module, app_name, namespace = include(manifest.api_urls)
            patterns = module.urlpatterns if hasattr(module, "urlpatterns") else module
            guarded = guard_application_api_urls(patterns, manifest)
            result.append(
                path(
                    f"api/v1/o/<uuid:organization_id>/apps/{manifest.app_label}/",
                    include((guarded, app_name), namespace=namespace),
                )
            )
    return result
