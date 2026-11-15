"""Organization sites (#477): the base domain, the classification of a host and the site index.

A host restricts what a request reaches, it never grants access: membership, RLS and
authorization stay the ones of ``/o/<uuid>/``. Nothing here opens ``system_context`` and no
model is imported at module level (the settings import this module's neighbours).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

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


@dataclass(frozen=True)
class Site:
    """The organization as seen through its own host: only what the administrator published (#480)."""

    organization_id: UUID
    slug: str
    name: str  # display name of the index; Organization.name when empty
    brand_color: str
    logo_key: str
    default_language: str
    icon_key: str = ""  # The square PNG icon (#485).


BRAND_FIELDS = ("display_name", "brand_color", "logo_key", "default_language", "icon_key")


def sync_site(organization: Any, settings_record: Any) -> None:
    """Mirror the published brand into ``OrganizationSite``; call inside the settings transaction and lock.

    Turning the address on creates (or refreshes) the row; turning it off keeps the row with
    ``enabled=False`` and an empty brand. Nothing is written when nothing changed.
    """
    from .models import OrganizationSite

    row = OrganizationSite.objects.select_for_update().filter(site_organization=organization).first()
    if settings_record is not None and settings_record.site_enabled:
        values = {"enabled": True, **{name: getattr(settings_record, name) for name in BRAND_FIELDS}}
    else:
        values = {"enabled": False, **dict.fromkeys(BRAND_FIELDS, "")}
        if row is None:
            return
    if row is None:
        OrganizationSite(site_organization=organization, **values).save()
        return
    changed = {name for name, value in values.items() if getattr(row, name) != value}
    for name in changed:
        setattr(row, name, values[name])
    if changed:
        row.save(update_fields=changed)


def _live_sites():
    from .models import OrganizationSite

    return OrganizationSite.objects.filter(
        enabled=True, site_organization__is_active=True, site_organization__deleted_at__isnull=True
    ).select_related("site_organization")


def _as_site(row: Any) -> Site | None:
    if row is None:
        return None
    organization = row.site_organization
    return Site(
        organization_id=organization.pk,
        slug=organization.slug,
        name=row.display_name or organization.name,
        brand_color=row.brand_color,
        logo_key=row.logo_key,
        default_language=row.default_language,
        icon_key=row.icon_key,
    )


def find_site(label: str) -> Site | None:
    """The enabled site of an active organization by its slug; ``label`` comes lowercased from ``classify_host``."""
    if not label:
        return None
    return _as_site(_live_sites().filter(site_organization__slug=label).first())


def site_of(organization_id: Any) -> Site | None:
    """The enabled site of an active organization by its id, or ``None``."""
    if organization_id is None:
        return None
    return _as_site(_live_sites().filter(site_organization_id=organization_id).first())


def shares_session(host: str) -> bool:
    """True when ``host`` (with or without port) is the base domain or one of its subdomains (#481)."""
    base = base_domain()
    name = _hostname(host or "")
    return bool(base) and bool(name) and (name == base or name.endswith("." + base))
