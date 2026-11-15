"""Template tags of the organization address (#486): the login providers and the SSO round trip."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from django import template
from django.conf import settings
from django.utils.http import url_has_allowed_host_and_scheme

register = template.Library()


def _provider_key(provider: Any) -> str:
    return getattr(provider, "sub_id", None) or provider.id


@register.simple_tag(takes_context=True)
def site_providers(context: Any, providers: Any) -> list[Any]:
    """The providers to offer: all of them on the main domain; on an organization host only the general
    ones and the host's own (presentation only: who may sign in stays the adapter's rule)."""
    site = context.get("organization_site")
    if site is None:
        return list(providers or ())
    from skeleton.accounts.providers import provider_organization_id

    kept = []
    for provider in providers or ():
        bound = provider_organization_id(_provider_key(provider))
        if bound is None or bound == site.organization_id:
            kept.append(provider)
    return kept


@register.simple_tag(takes_context=True)
def site_sso(context: Any) -> dict[str, Any]:
    """``enabled``, ``platform_origin`` and ``next`` for OIDC/SAML started on an organization host.

    The identity providers only know the main domain's callback, so the button is a link to the
    main domain with an absolute ``next`` back to this host (accepted by ``AccountAdapter.is_safe_url``).
    """
    from skeleton.tenancy.site import organization_origin, shares_session

    request = context.get("request")
    site = context.get("organization_site")
    public = (getattr(settings, "SKELETON_PUBLIC_URL", "") or "").strip().rstrip("/")
    public_host = urlsplit(public).netloc
    origin = organization_origin(site.slug) if site is not None else None
    if request is None or origin is None or not public_host:
        return {"enabled": False, "platform_origin": "", "next": ""}
    host = request.get_host()
    wanted = request.GET.get("next", "")
    path = wanted if wanted.startswith("/") and url_has_allowed_host_and_scheme(wanted, allowed_hosts=None) else "/"
    return {
        # A custom domain (#489) never shares the session, so it never offers OIDC/SAML.
        "enabled": shares_session(host) and shares_session(public_host),
        "platform_origin": public,
        "next": origin + path,
    }


@register.simple_tag(takes_context=True)
def provider_site_login_url(context: Any, provider: Any, sso: dict[str, Any]) -> str:
    """The provider's login URL on the main domain, returning to this organization host."""
    request = context.get("request")
    return sso["platform_origin"] + provider.get_login_url(request, process="login", next=sso["next"])


@register.simple_tag(takes_context=True)
def site_for_next(context: Any) -> Any:
    """The site that the ``next`` of this request returns to, when it is an organization address."""
    from skeleton.tenancy.site import classify_host, find_site, is_site_url

    request = context.get("request")
    wanted = request.GET.get("next", "") if request is not None else ""
    if not is_site_url(wanted):
        return None
    return find_site(classify_host(urlsplit(wanted).netloc).label)
