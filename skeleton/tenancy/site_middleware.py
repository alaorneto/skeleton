"""Middlewares of the organization sites (#477): the shared session cookie and the host routing."""

from __future__ import annotations

from django.conf import settings
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.exceptions import DisallowedHost

from .site import base_domain, shares_session


class SiteSessionMiddleware(SessionMiddleware):
    """Session cookie shared by every host under the base domain; host-only everywhere else (#481).

    The domain is decided per request: a browser accepts ``Domain=.<base>`` only from a host
    inside the base domain, and a custom domain (#489) must keep a host-only cookie. The CSRF
    cookie is never shared: each host gets its own and the ``Origin`` check stays per host.
    """

    def process_response(self, request, response):
        response = super().process_response(request, response)
        name = settings.SESSION_COOKIE_NAME
        if name in response.cookies:
            try:
                host = request.get_host()
            except DisallowedHost:
                return response
            if shares_session(host):
                response.cookies[name]["domain"] = "." + base_domain()
        return response


# Global routes reachable on an organization host (#482): identity and the person's own pages.
# Platform routes and routes over many organizations are refused; when in doubt, refuse.
ALLOWED_GLOBAL_PREFIXES = ("/accounts/", "/profile/", "/me/", "/api/v1/", "/site/", "/health/")
ALLOWED_GLOBAL_PATHS = frozenset({"/sw.js", "/favicon.ico"})


def refused(request):
    """The same neutral 404 for an unknown, disabled, reserved or foreign host or path, whatever the method."""
    from django.http import HttpResponseNotFound

    request.skeleton_site_refused = True
    return HttpResponseNotFound("Not found.")


def _query(request):
    query = request.META.get("QUERY_STRING", "")
    return "?" + query if query else ""


def allowed_global(path, site):
    """A global route that the organization host may serve (identity, the person's pages, health)."""
    prefixes = list(ALLOWED_GLOBAL_PREFIXES)
    static = getattr(settings, "STATIC_URL", "") or ""
    if static.startswith("/"):
        prefixes.append(static)
    prefixes += [f"/invite/{site.organization_id}/", f"/join/{site.organization_id}/"]
    prefixes += [prefix for prefix in getattr(settings, "SKELETON_ORGANIZATION_SITE_EXTRA_PREFIXES", ()) if prefix]
    return path in ALLOWED_GLOBAL_PATHS or path.startswith(tuple(prefixes))


def route_for_site(request, site):
    """Rewrite, redirect or refuse a request on the organization host; ``None`` lets it through."""
    from django.http import HttpResponsePermanentRedirect, HttpResponseRedirect

    from .routing import is_org_route

    prefix = f"/o/{site.organization_id}"
    path = request.path_info
    if path.startswith(("/o/", "/api/v1/o/")):
        parts = path.split("/")
        explicit = parts[2] if path.startswith("/o/") else (parts[4] if len(parts) > 4 else "")
        if explicit.lower() != str(site.organization_id):
            return refused(request)
        if path.startswith(prefix + "/") and request.method in ("GET", "HEAD") and is_org_route(path):
            return HttpResponseRedirect(path[len(prefix) :] + _query(request))
        return None
    if is_org_route(prefix + path):
        # The organization route always wins over a global one (/, /notifications/, /flows/tasks/).
        # Only path_info changes: request.path stays clean for templates and ?next=.
        request.path_info = prefix + path
        return None
    if (
        settings.APPEND_SLASH
        and request.method in ("GET", "HEAD")
        and not path.endswith("/")
        and is_org_route(prefix + path + "/")
    ):
        return HttpResponsePermanentRedirect(path + "/" + _query(request))
    if allowed_global(path, site):
        return None
    return refused(request)


class OrganizationSiteMiddleware:
    """Serve ``<slug>.<base domain>`` as its organization (#482); the host restricts, never grants.

    The site comes only from ``request.get_host()``, already validated by ``ALLOWED_HOSTS``;
    membership, RLS and authorization stay the tenant context's. Nothing here opens
    ``system_context``.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from .routing import site_scope
        from .site import classify_host, find_site, site_of

        try:
            host = request.get_host()
        except DisallowedHost:
            return self.get_response(request)  # CommonMiddleware answers 400, as before.
        kind = classify_host(host)
        if kind.kind == "platform":
            if _dynamic_only(host):
                # Allowed by the custom domain cache but no longer classified (the cache expired in
                # between): never serve the platform on a customer's domain (#489).
                return refused(request)
            return self.get_response(request)
        site = None
        if kind.kind == "organization":
            site = find_site(kind.label) if kind.label else site_of(kind.organization_id)
        if site is None:
            return refused(request)
        request.organization_site = site
        with site_scope(site.organization_id):
            early = route_for_site(request, site)
            response = early if early is not None else self.get_response(request)
        if not kind.label:
            _custom_domain_hsts(request, response)
        return response


def _dynamic_only(host):
    """True when ``host`` passed ``ALLOWED_HOSTS`` only through the custom domains."""
    from django.http.request import split_domain_port, validate_host

    from .site import _static_allowed_hosts
    from .site_domains import custom_domains_enabled

    if not custom_domains_enabled():
        return False
    return not validate_host(split_domain_port(host)[0], _static_allowed_hosts())


def _custom_domain_hsts(request, response):
    """HSTS without includeSubDomains/preload on a customer's domain: it must not bind their other hosts."""
    seconds = getattr(settings, "SECURE_HSTS_SECONDS", 0)
    if seconds and request.is_secure():
        response.headers["Strict-Transport-Security"] = f"max-age={seconds}"
