"""The timeline of a record (#185): audit, attachments and registered sources, newest first.

Each source authorizes its own items for the person who looks, every time:

- audit events only for whoever reads the record history (organization administrator, or
  TablePermission ``audit`` with read access to the record), with the changes cut to the
  ``read_fields`` exactly as on the audit page;
- attachments by the read policy of the target record (``attachments_for``);
- other sources (comments, #187) through :func:`register_timeline_source`.

Pages use a keyset cursor over ``(occurred_at, sysid)``: each source returns at most one page
older than the cursor, the merge keeps the newest ``page_size`` items and the last of them is
the next cursor, so a page never costs more than one bounded query per source.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from django.core.exceptions import ValidationError

PAGE_SIZE = 20


@dataclass(frozen=True)
class TimelineItem:
    """One entry; ``data`` holds already authorized, display-ready values.

    ``template`` renders an item of a registered source (it receives ``item`` and closes the
    header ``div`` opened by the timeline); audit and attachments have built-in markup.
    """

    kind: str
    occurred_at: datetime
    sysid: UUID
    data: dict[str, Any] = field(default_factory=dict)
    template: str = ""

    @property
    def key(self) -> tuple[datetime, str]:
        return (self.occurred_at, str(self.sysid))


#: ``source(spec, record, *, user, before, limit) -> list[TimelineItem]``; ``before`` is the
#: cursor ``(occurred_at, sysid)`` or None. A source returns items strictly older than it.
TimelineSource = Callable[..., list[TimelineItem]]
_sources: dict[str, TimelineSource] = {}


def register_timeline_source(kind: str, source: TimelineSource) -> None:
    """Add a kind of item to every record timeline (in ``AppConfig.ready()`` of a core subsystem)."""
    existing = _sources.get(kind)
    if existing is not None and existing is not source:
        raise ValueError(f"Duplicate timeline source: {kind}")
    _sources[kind] = source


def timeline_sources() -> dict[str, TimelineSource]:
    return dict(_sources)


def encode_cursor(item: TimelineItem) -> str:
    raw = f"{item.occurred_at.isoformat()}|{item.sysid}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    if cursor in (None, ""):
        return None
    try:
        padded = str(cursor) + "=" * (-len(str(cursor)) % 4)
        moment, identifier = base64.urlsafe_b64decode(padded.encode()).decode().split("|")
        value = datetime.fromisoformat(moment)
        if value.tzinfo is None:
            raise ValueError
        return value, UUID(identifier)
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError):
        raise ValidationError({"cursor": "Cursor inválido."}) from None


def _older(queryset: Any, before: tuple[datetime, UUID] | None, *, time_field: str) -> Any:
    from django.db.models import Q

    if before is None:
        return queryset
    moment, identifier = before
    return queryset.filter(Q(**{f"{time_field}__lt": moment}) | Q(**{time_field: moment, "sysid__lt": identifier}))


def audit_items(spec: Any, record: Any, *, user: Any, before: Any, limit: int) -> list[TimelineItem]:
    from skeleton.access.policies import authorized_audit_queryset
    from skeleton.audit.presentation import visible_audit_changes
    from skeleton.service_api.audit import history_allowed

    if not history_allowed(spec, user, record.pk):
        return []
    events = authorized_audit_queryset(user).filter(model_label=spec.model._meta.label_lower, record_sysid=record.pk)
    events = _older(events, before, time_field="created_at").order_by("-created_at", "-sysid")[:limit]
    return [
        TimelineItem(
            "audit",
            event.created_at,
            event.pk,
            {
                "operation": event.operation,
                "revision": event.record_revision,
                "actor_sysid": event.actor_sysid,
                "changes": visible_audit_changes(event),
            },
        )
        for event in events
    ]


def attachment_items(spec: Any, record: Any, *, user: Any, before: Any, limit: int) -> list[TimelineItem]:
    from django.http import Http404

    from skeleton.files import services

    if spec.attachments is None:
        return []
    try:
        rows = services.attachments_for(spec, record.pk, user=user)
    except Http404:
        return []
    rows = _older(rows, before, time_field="created_at").order_by("-created_at", "-sysid")[:limit]
    return [
        TimelineItem(
            "attachment",
            row.created_at,
            row.pk,
            {"filename": row.filename, "size": row.size, "state": row.state, "uploaded_by": row.created_by},
        )
        for row in rows
    ]


register_timeline_source("audit", audit_items)
register_timeline_source("attachment", attachment_items)


def record_timeline(
    spec: Any, record: Any, *, user: Any, cursor: str | None = None, page_size: int = PAGE_SIZE
) -> tuple[list[TimelineItem], str | None]:
    """One page of the record's timeline for ``user`` and the cursor of the next page (or None)."""
    if type(page_size) is not int or not 1 <= page_size <= 100:
        raise ValidationError({"page_size": "Tamanho de página inválido."})
    before = decode_cursor(cursor)
    items: list[TimelineItem] = []
    for source in _sources.values():
        items.extend(source(spec, record, user=user, before=before, limit=page_size + 1))
    items.sort(key=lambda item: item.key, reverse=True)
    page = items[:page_size]
    return page, encode_cursor(page[-1]) if len(items) > page_size else None
