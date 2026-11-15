"""Organization sites (#477): the base domain, the classification of a host and the site index.

A host restricts what a request reaches, it never grants access: membership, RLS and
authorization stay the ones of ``/o/<uuid>/``. Nothing here opens ``system_context`` and no
model is imported at module level (the settings import this module's neighbours).
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from django.conf import settings
from django.http.request import split_domain_port

from skeleton.common.hostnames import slug_problem


@dataclass(frozen=True)
class HostKind:
    kind: str  # "platform" | "organization" | "unknown"
    label: str = ""  # the slug, when kind == "organization"


PLATFORM = HostKind("platform")
UNKNOWN = HostKind("unknown")


def base_domain() -> str:
    """``SKELETON_ORGANIZATION_BASE_DOMAIN``, normalized; empty when the feature is off."""
    return (getattr(settings, "SKELETON_ORGANIZATION_BASE_DOMAIN", "") or "").strip().strip(".").lower()


def _hostname(host: str) -> str:
    return split_domain_port(host.lower())[0].rstrip(".")


def _static_allowed_hosts() -> list[str]:
    """The configured hosts, never the dynamic custom domains (#489)."""
    return list(list.__iter__(settings.ALLOWED_HOSTS))


def _public_parts():
    return urlsplit((getattr(settings, "SKELETON_PUBLIC_URL", "") or "").strip())


def platform_hosts() -> frozenset[str]:
    """Hosts that always behave as the platform: the public URL, health, the bare base domain, explicit hosts."""
    names = set()
    public = _public_parts().hostname
    if public:
        names.add(public.lower().rstrip("."))
    health = getattr(settings, "SKELETON_HEALTH_HOST", "")
    if health:
        names.add(_hostname(health))
    base = base_domain()
    if base:
        names.add(base)
    for entry in _static_allowed_hosts():
        if entry and not entry.startswith(".") and entry != "*":
            names.add(_hostname(entry))
    return frozenset(names)


def classify_host(host: str) -> HostKind:
    """Platform, organization (with its label) or unknown; see the rules in docs/organization-sites.md."""
    name = _hostname(host or "")
    base = base_domain()
    if not base:
        return PLATFORM
    if host and not name:
        return UNKNOWN  # Not a valid host name at all (Django refuses it before any view).
    if name in platform_hosts():
        return PLATFORM
    suffix = "." + base
    if name.endswith(suffix):
        front = name[: -len(suffix)]
        if "." in front or not front:
            return UNKNOWN
        return HostKind("organization", front) if slug_problem(front) is None else UNKNOWN
    return PLATFORM


def _origin_for(hostname: str) -> str | None:
    parts = _public_parts()
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    return f"{parts.scheme}://{hostname}" + (f":{port}" if port else "")


def organization_origin(slug: str) -> str | None:
    """``https://<slug>.<base>`` (with the public URL's scheme and port), or ``None`` when unavailable."""
    base = base_domain()
    if not base or slug_problem(slug) is not None:
        return None
    return _origin_for(f"{slug}.{base}")
