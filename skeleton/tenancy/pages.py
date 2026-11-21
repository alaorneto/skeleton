"""The configurable pages of an organization (#623, #624): the public page and the internal home.

``organization_page(slot)`` reads the page of the context organization (tenant context, RLS).
``save_organization_page`` is the only write: the organization administrator, under
``organization_lock(actor)``, CAS on the page revision (0 before the row exists), the document
validated by ``skeleton.common.page_blocks`` with the kind of the slot. Saving the public page
refreshes its global mirror (``OrganizationSitePage``) in the same transaction, through
``sync_site_page``. ``published_site_page`` reads only that mirror, without a tenant context and
without ``system_context``, for the anonymous visitor of the organization's own address.
"""

from __future__ import annotations

from typing import Any

from django.db import IntegrityError, transaction

from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.common.page_blocks import ORGANIZATION_HOME, ORGANIZATION_PUBLIC, PageState, empty_page, validate_page

from .site import published_site_page

__all__ = ["SLOT_KINDS", "organization_page", "published_site_page", "save_organization_page"]

SLOT_KINDS = {"public": ORGANIZATION_PUBLIC, "home": ORGANIZATION_HOME}


def _state(row: Any) -> PageState:
    if row is None:
        return PageState()
    document = row.document if isinstance(row.document, dict) and row.document else empty_page()
    return PageState(document=document, published=row.published, revision=row.revision)


def _slot(slot: str) -> str:
    if slot not in SLOT_KINDS:
        raise ValueError(f"Unknown page slot: {slot!r}.")
    return slot


def organization_page(slot: str) -> PageState:
    """The page ``slot`` of the context organization; an empty, unpublished page without a row."""
    from .models import OrganizationPage

    return _state(OrganizationPage.objects.filter(slot=_slot(slot)).first())


def known_widgets() -> frozenset[str]:
    """Every registered widget name: turning an app off never invalidates a page (#624)."""
    from skeleton.system.dashboard_widgets import dashboard_widgets

    return frozenset(widget.name for widget in dashboard_widgets())


def save_organization_page(
    *, actor: Any, slot: str, document: Any, published: bool, expected_revision: int
) -> PageState:
    """Replace the page ``slot`` with CAS; only the organization administrator.

    Raises ``PermissionDenied`` (not the administrator), ``ValidationError`` (by block and field)
    or ``ConcurrentUpdate`` (another revision). An unpublished page may have no block. Equal
    values write nothing.
    """
    from skeleton.access.services import organization_lock

    from .models import OrganizationPage
    from .site import sync_site_page

    kind = SLOT_KINDS[_slot(slot)]
    if type(expected_revision) is not int:
        raise ConcurrentUpdate("A revision is required.")
    if type(published) is not bool:
        raise TypeError("published must be a bool.")
    widgets = known_widgets() if kind == ORGANIZATION_HOME else frozenset()
    normalized = validate_page(document, kind=kind, known_widgets=widgets, allow_empty=not published)
    with organization_lock(actor) as organization:
        row = OrganizationPage.objects.select_for_update().filter(slot=slot).first()
        if (row.revision if row is not None else 0) != expected_revision:
            raise ConcurrentUpdate("Organization page changed.")
        if row is None:
            row = OrganizationPage(organization=organization, slot=slot, document=normalized, published=published)
            try:
                with transaction.atomic():
                    row.save()
            except IntegrityError:
                raise ConcurrentUpdate("Organization page created concurrently.") from None
        else:
            changed = {
                name
                for name, value in (("document", normalized), ("published", published))
                if getattr(row, name) != value
            }
            if not changed:
                return _state(row)
            row.document, row.published = normalized, published
            row.save(update_fields=changed)
        if slot == "public":
            sync_site_page(organization)
        return _state(row)
