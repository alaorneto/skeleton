"""The public home page of the instance (#622): one page of structured blocks at ``/`` of the platform.

``home_page()`` is what ``/`` reads for whoever has not signed in: cached per process like the
appearance snapshot (``TTL_S`` seconds; a database error keeps the last good value for
``STALE_S`` seconds, then answers "no page"; setup mode always answers "no page"). Every write
resets the cache after its commit, so the process that writes sees the change at once and the
others within the TTL. ``pin_home_page()`` fixes the value in tests (``tests/conftest.py`` pins
"no page", so query budgets do not change).

``save_home_page`` is the only write: the full platform administrator, with CAS, the document
validated by ``skeleton.common.page_blocks`` (``kind="platform_home"``), and a ``security_event``
that never carries the content. The page is public by definition: it never reads anything of an
organization or of a person.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from django.db import IntegrityError, transaction

from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.common.page_blocks import PLATFORM_HOME, PageState, empty_page, validate_page

__all__ = [
    "current_home_page",
    "home_page",
    "pin_home_page",
    "reset_home_page_cache",
    "save_home_page",
]

logger = logging.getLogger("skeleton.appearance")

TTL_S = 5.0
STALE_S = 300.0
NO_PAGE = PageState()
_CACHE: dict[str, Any] = {"value": None, "at": 0.0, "good": None, "good_at": 0.0, "pinned": None}


def reset_home_page_cache() -> None:
    """Forget the cached page (after a write commits, and in tests); a pin stays."""
    _CACHE.update(value=None, at=0.0, good=None, good_at=0.0)


def pin_home_page(value: PageState | None) -> None:
    """Tests only: always answer ``value`` (``None`` removes the pin)."""
    _CACHE["pinned"] = value


def _row_state(row: Any) -> PageState:
    if row is None:
        return NO_PAGE
    document = row.document if isinstance(row.document, dict) and row.document else empty_page()
    return PageState(document=document, published=row.published, revision=row.revision)


def current_home_page() -> PageState:
    """The stored page, read now (the editor: it needs the current revision, never a cached one)."""
    from .models import HOME_SLOT, InstancePage

    return _row_state(InstancePage.objects.filter(slot=HOME_SLOT).first())


def home_page() -> PageState:
    """The page ``/`` shows to anonymous visitors, read at most every ``TTL_S`` seconds per process."""
    if _CACHE["pinned"] is not None:
        return _CACHE["pinned"]
    from django.apps import apps
    from django.conf import settings

    if not apps.ready or getattr(settings, "SKELETON_SETUP_MODE", False):
        return NO_PAGE
    now = time.monotonic()
    if _CACHE["value"] is not None and now - _CACHE["at"] < TTL_S:
        return _CACHE["value"]
    from django.db import DatabaseError

    try:
        with transaction.atomic():  # A failed read never breaks the transaction of the request.
            value = current_home_page()
    except DatabaseError:
        logger.warning("home_page.unavailable")  # Never the content in logs.
        if _CACHE["good"] is not None and now - _CACHE["good_at"] < STALE_S:
            return _CACHE["good"]
        return NO_PAGE
    _CACHE.update(value=value, at=now, good=value, good_at=now)
    return value


def save_home_page(*, actor: Any, document: Any, published: bool, expected_revision: int) -> PageState:
    """Replace the page with CAS; only the full platform administrator (``PermissionDenied`` otherwise).

    An unpublished page may have no block; a published one needs from 1 to 20. Equal values
    write nothing. Raises ``ValidationError`` (by block and field) or ``ConcurrentUpdate``.
    """
    from skeleton.common.logging import security_event

    from .models import HOME_SLOT, InstancePage
    from .services import _check_revision, _require_platform

    _require_platform(actor)
    _check_revision(expected_revision)
    if type(published) is not bool:
        raise TypeError("published must be a bool.")
    normalized = validate_page(document, kind=PLATFORM_HOME, allow_empty=not published)
    with transaction.atomic():
        row = InstancePage.objects.select_for_update().filter(slot=HOME_SLOT).first()
        if (row.revision if row is not None else 0) != expected_revision:
            raise ConcurrentUpdate("Instance page changed.")
        if row is None:
            row = InstancePage(slot=HOME_SLOT, document=normalized, published=published)
            try:
                with transaction.atomic():
                    row.save()
            except IntegrityError:
                raise ConcurrentUpdate("Instance page created concurrently.") from None
        else:
            changed = {
                name
                for name, value in (("document", normalized), ("published", published))
                if getattr(row, name) != value
            }
            if not changed:
                return _row_state(row)
            row.document, row.published = normalized, published
            row.save(update_fields=changed)
        transaction.on_commit(reset_home_page_cache)
    security_event(
        "appearance.home_page_changed",
        reason_code="published" if published else "draft",
        count=len(normalized["blocks"]),
        source=f"platform:{actor.pk}" if actor is not None else "console",
    )
    return _row_state(row)
