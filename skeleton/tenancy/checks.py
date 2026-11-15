"""System checks of the organization sites (#477)."""

from urllib.parse import urlsplit

from django.conf import settings
from django.core.checks import Error, Tags, register


def base_domain_problems():
    """skeleton.E038: a base domain that is not a domain, or one without the public URL it derives from."""
    raw = getattr(settings, "SKELETON_ORGANIZATION_BASE_DOMAIN", "") or ""
    if not str(raw).strip():
        return []
    from skeleton.instance.schema import _base_domain

    problems = []
    try:
        _base_domain(str(raw))
    except ValueError:
        problems.append(
            "SKELETON_ORGANIZATION_BASE_DOMAIN is not a bare domain such as skeleton.com "
            "(no scheme, path, port, wildcard or address)."
        )
    public = urlsplit(str(getattr(settings, "SKELETON_PUBLIC_URL", "") or "").strip())
    if public.scheme not in {"http", "https"} or not public.hostname:
        problems.append(
            "SKELETON_ORGANIZATION_BASE_DOMAIN is set without a SKELETON_PUBLIC_URL origin: the organization "
            "addresses take their scheme and port from it."
        )
    return problems


@register(Tags.security)
def organization_site_checks(app_configs=None, **kwargs):
    return [Error(problem, id="skeleton.E038") for problem in base_domain_problems()]


def session_problems():
    """skeleton.E039 / W039: the core decides the session cookie domain per request (#481)."""
    from .site import base_domain, shares_session

    if not base_domain():
        return [], []
    errors, warnings = [], []
    configured = [name for name in ("SESSION_COOKIE_DOMAIN", "CSRF_COOKIE_DOMAIN") if getattr(settings, name, None)]
    if configured:
        errors.append(
            f"{' and '.join(configured)} set with SKELETON_ORGANIZATION_BASE_DOMAIN: the core decides the "
            "session cookie domain per request and never shares the CSRF cookie. Remove the setting."
        )
    public = urlsplit(str(getattr(settings, "SKELETON_PUBLIC_URL", "") or "").strip()).hostname
    if public and not shares_session(public):
        warnings.append(
            "The host of SKELETON_PUBLIC_URL is outside SKELETON_ORGANIZATION_BASE_DOMAIN: the session is not "
            "shared between the main domain and the organization addresses, and single sign-on is not offered there."
        )
    return errors, warnings


@register(Tags.security)
def organization_session_checks(app_configs=None, **kwargs):
    from django.core.checks import Warning as CheckWarning

    errors, warnings = session_problems()
    return [Error(problem, id="skeleton.E039") for problem in errors] + [
        CheckWarning(problem, id="skeleton.W039") for problem in warnings
    ]
