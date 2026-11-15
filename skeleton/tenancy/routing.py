"""Organization route prefix: ``/o/<uuid>/`` on the platform host, empty on the organization's own host (#479).

Every web route of an organization starts with ``ORG`` instead of the fixed text
``o/<uuid:organization_id>/``. Outside a site scope ``resolve`` and ``reverse`` give exactly
the canonical URLs; inside ``site_scope(U)`` (opened only by the host middleware, #482) the
URLs of ``U`` come out clean and other organizations are never matched. The site scope is a
second barrier: the tenant context still validates membership on every request.
"""

from __future__ import annotations

import contextvars
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from django.urls import Resolver404, register_converter, resolve

ORG = "<org:organization_id>"
_UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_SITE: contextvars.ContextVar[uuid.UUID | None] = contextvars.ContextVar("skeleton_site_organization", default=None)


def site_organization_id() -> uuid.UUID | None:
    """The organization of the current host, or ``None`` on the platform host and outside requests."""
    return _SITE.get()


@contextmanager
def site_scope(organization_id: object) -> Iterator[None]:
    """Bind the organization of the host for the duration of one request; always restored."""
    token = _SITE.set(uuid.UUID(str(organization_id)))
    try:
        yield
    finally:
        _SITE.reset(token)


def organization_prefix(organization_id: object) -> str:
    """``"/"`` when ``organization_id`` is the site's organization, ``"/o/<uuid>/"`` otherwise."""
    value = uuid.UUID(str(organization_id))
    return "/" if _SITE.get() == value else f"/o/{value}/"


class OrganizationPrefixConverter:
    regex = f"(?:o/{_UUID}/)?"

    def to_python(self, value: str) -> uuid.UUID:
        site = _SITE.get()
        if value == "":
            if site is None:
                raise ValueError("clean organization URLs exist only on an organization host")
            return site
        found = uuid.UUID(value[2:-1])
        if site is not None and found != site:
            raise ValueError("another organization is not reachable from this host")
        return found

    def to_url(self, value: object) -> str:
        found = uuid.UUID(str(value))
        return "" if _SITE.get() == found else f"o/{found}/"


register_converter(OrganizationPrefixConverter, "org")


def is_org_route(path: str) -> bool:
    """True when ``path`` resolves to a web route of an organization (its route has ``ORG``)."""
    try:
        match = resolve(path)
    except Resolver404:
        return False
    return ORG in (match.route or "")
