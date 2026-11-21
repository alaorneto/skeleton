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
    label: str = ""  # the slug, when kind == "organization" under the base domain
    organization_id: UUID | None = None  # a verified custom domain (#489)


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
    from .site_domains import custom_hosts

    organization_id = custom_hosts().get(name)
    if organization_id is not None:
        return HostKind("organization", "", organization_id)
    return PLATFORM


class SiteAllowedHosts(list):
    """The static allowed hosts plus the verified custom domains (#489); never a wildcard.

    Django validates a host by iterating ``ALLOWED_HOSTS``: the dynamic part is appended at
    iteration time from the per-process cache of ``SiteDomainClaim``. ``"*" in`` and indexing
    see only the static part.
    """

    def __iter__(self):
        yield from super().__iter__()
        from .site_domains import custom_hosts

        yield from (host for host in custom_hosts() if host and not host.startswith(".") and "*" not in host)


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
    custom_domain: str = ""  # The oldest verified custom domain (#489), or "".


BRAND_FIELDS = ("display_name", "brand_color", "logo_key", "default_language", "icon_key")


def sync_site(organization: Any, settings_record: Any) -> None:
    """Mirror the published brand into ``OrganizationSite``; call inside the settings transaction and lock.

    Turning the address on creates (or refreshes) the row; turning it off keeps the row with
    ``enabled=False`` and an empty brand. Nothing is written when nothing changed.
    """
    from .models import OrganizationSite

    enabled = settings_record is not None and settings_record.site_enabled
    sync_site_page(organization, site_enabled=enabled)  # The public page follows the address (#623).
    row = OrganizationSite.objects.select_for_update().filter(site_organization=organization).first()
    if enabled:
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


def sync_site_page(organization: Any, *, site_enabled: bool | None = None) -> None:
    """Mirror the published public page into ``OrganizationSitePage`` (#623); same transaction and lock.

    Call it inside the tenant context of ``organization``, under ``organization_lock``, from the
    write that changes the page (``skeleton.tenancy.pages``) or the address (``sync_site``).
    ``enabled`` is "address on and page published"; off, the document is emptied. Without
    ``site_enabled`` the stored settings decide. Nothing is written when nothing changed.
    """
    from .models import OrganizationPage, OrganizationSettings, OrganizationSitePage

    if site_enabled is None:
        settings_row = OrganizationSettings.objects.filter(organization=organization).first()
        site_enabled = bool(settings_row is not None and settings_row.site_enabled)
    page = OrganizationPage.objects.filter(organization=organization, slot="public").first()
    enabled = bool(site_enabled and page is not None and page.published and page.document)
    values = {"enabled": enabled, "document": page.document if enabled and page is not None else {}}
    row = OrganizationSitePage.objects.select_for_update().filter(site_organization=organization).first()
    if row is None:
        if enabled:
            OrganizationSitePage(site_organization=organization, **values).save()
        return
    changed = {name for name, value in values.items() if getattr(row, name) != value}
    for name in changed:
        setattr(row, name, values[name])
    if changed:
        row.save(update_fields=changed)


def published_site_page(organization_id: Any) -> dict[str, Any] | None:
    """The published public page of an organization with its address on, from the mirror only (#623).

    No tenant context and no ``system_context``: like ``site_of``, it reads the global index.
    ``None`` when the address is off, the page is not published or the organization is inactive.
    """
    from .models import OrganizationSitePage

    if organization_id is None:
        return None
    document = (
        OrganizationSitePage.objects.filter(
            site_organization_id=organization_id,
            enabled=True,
            site_organization__is_active=True,
            site_organization__deleted_at__isnull=True,
        )
        .values_list("document", flat=True)
        .first()
    )
    return document if isinstance(document, dict) and document.get("blocks") else None


def _live_sites():
    from .models import OrganizationSite

    return OrganizationSite.objects.filter(
        enabled=True, site_organization__is_active=True, site_organization__deleted_at__isnull=True
    ).select_related("site_organization")


def _as_site(row: Any) -> Site | None:
    if row is None:
        return None
    organization = row.site_organization
    from .models import SiteDomainClaim

    custom = ""
    from .site_domains import custom_domains_enabled

    if custom_domains_enabled():
        custom = (
            SiteDomainClaim.objects.filter(claiming_organization_id=organization.pk)
            .order_by("created_at", "domain")
            .values_list("domain", flat=True)
            .first()
            or ""
        )
    return Site(
        organization_id=organization.pk,
        slug=organization.slug,
        name=row.display_name or organization.name,
        brand_color=row.brand_color,
        logo_key=row.logo_key,
        default_language=row.default_language,
        icon_key=row.icon_key,
        custom_domain=custom,
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


def is_site_url(url: str) -> bool:
    """True only for an absolute URL on the address of an organization whose site is enabled (#486).

    Same scheme and port as ``SKELETON_PUBLIC_URL``, no credentials, no backslash or control
    character, a single label under the base domain and an enabled site. Custom domains are
    never accepted here.
    """
    from django.utils.http import url_has_allowed_host_and_scheme

    if not isinstance(url, str) or not url or "\\" in url or any(ord(char) < 32 or ord(char) == 127 for char in url):
        return False
    parts = urlsplit(url)
    public = _public_parts()
    if not parts.netloc or "@" in parts.netloc or parts.scheme != public.scheme or not public.scheme:
        return False
    try:
        if parts.port != public.port:
            return False
    except ValueError:
        return False
    kind = classify_host(parts.netloc)
    if kind.kind != "organization" or not kind.label or find_site(kind.label) is None:
        return False
    return url_has_allowed_host_and_scheme(url, allowed_hosts={parts.netloc}, require_https=parts.scheme == "https")


def site_origin(site: Site) -> str | None:
    """Origin of the organization's own address (#487): the verified custom domain (#489), else the slug host."""
    if site.custom_domain and base_domain():
        return _origin_for(site.custom_domain)
    return organization_origin(site.slug)


_NO_SITE = object()


def _site_for_links(organization_id: Any) -> Site | None:
    """``site_of`` memoized for the unit of work (a request or a job context): one index read per organization."""
    from .context import request_cache

    cache = request_cache()
    key = ("skeleton.site_of", str(organization_id))
    if cache is not None and key in cache:
        value = cache[key]
        return None if value is _NO_SITE else value
    site = site_of(organization_id)
    if cache is not None:
        cache[key] = site if site is not None else _NO_SITE
    return site


def organization_url(organization_id: Any, path: str) -> str | None:
    """``path`` (as ``reverse`` gives it outside a request) on the organization's own address (#487).

    ``None`` when the organization has no enabled site: the caller keeps the platform URL. An
    organization route loses its ``/o/<uuid>`` prefix; global and API routes keep their path.
    """
    from .routing import is_org_route

    if organization_id is None or not base_domain():
        return None
    site = _site_for_links(organization_id)
    origin = site_origin(site) if site is not None else None
    if origin is None:
        return None
    prefix = f"/o/{site.organization_id}/"
    if path.startswith(prefix) and is_org_route(path):
        path = "/" + path[len(prefix) :]
    return origin + path


def shareable_url(request: Any, organization_id: Any, path: str) -> str | None:
    """A link about an organization that leaves the page (email, copied link): its own address when enabled.

    Falls back to the request's host, then to ``SKELETON_PUBLIC_URL``; ``None`` when neither exists.
    """
    url = organization_url(organization_id, path)
    if url is not None:
        return url
    if request is not None:
        return request.build_absolute_uri(path)
    origin = (getattr(settings, "SKELETON_PUBLIC_URL", "") or "").strip().rstrip("/")
    return origin + path if origin else None
