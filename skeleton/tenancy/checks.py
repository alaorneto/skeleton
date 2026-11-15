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
