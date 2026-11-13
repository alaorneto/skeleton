"""Timeline of a record (#185): audit and attachments in one list, each filtered for the reader.

Every source decides on its own what the reader may see, so joining them never widens
anything:

- ``audit`` goes through ``AuditService.history``, which needs the organization administrator
  or TablePermission ``audit`` plus reading the record, and trims changes to the visible columns;
- ``attachments`` lists the record's attachments by the target's authorization.

Comments (#187) and other core sources add themselves with ``register_timeline_source``. A source
receives ``(user, spec, record)`` and returns ``TimelineItem``s; it must return nothing it would
not show on its own.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from django.utils.translation import gettext

LIMIT = 50


@dataclass(frozen=True, slots=True)
class TimelineItem:
    at: datetime
    kind: str
    title: str
    detail: str = ""
    actor_id: Any = None
    url: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


TimelineSource = Callable[[Any, Any, Any], Iterable[TimelineItem]]
_sources: dict[str, TimelineSource] = {}


def register_timeline_source(name: str, source: TimelineSource) -> None:
    _sources[name] = source


def timeline_for(user: Any, spec: Any, record: Any) -> list[TimelineItem]:
    items: list[TimelineItem] = []
    for source in _sources.values():
        items.extend(source(user, spec, record))
    items.sort(key=lambda item: item.at, reverse=True)
    return items[:LIMIT]


def _audit(user: Any, spec: Any, record: Any) -> Iterable[TimelineItem]:
    from skeleton.audit.models import AuditEvent
    from skeleton.service_api.audit import AuditService
    from skeleton.service_api.errors import NotFound

    try:
        page = AuditService().history(
            actor=user, table_key=spec.model._meta.label_lower, record_id=record.pk, page_size=LIMIT
        )
    except NotFound:
        return []
    labels = dict(AuditEvent.Operation.choices)
    return [
        TimelineItem(
            at=entry.occurred_at,
            kind="audit",
            title=str(labels.get(entry.operation, entry.operation)),
            detail=", ".join(change.name for change in entry.changes),
            actor_id=entry.actor_id,
            extra={"revision": entry.record_revision},
        )
        for entry in page.items
    ]


def _attachments(user: Any, spec: Any, record: Any) -> Iterable[TimelineItem]:
    if spec.attachments is None:
        return []
    from skeleton.files import services

    return [
        TimelineItem(
            at=row.created_at,
            kind="attachment",
            title=gettext("Anexo adicionado"),
            detail=row.filename,
        )
        for row in services.attachments_for(spec, record.pk, user=user)
    ]


register_timeline_source("audit", _audit)
register_timeline_source("attachments", _attachments)
