"""Comments as items of the record timeline (#185 extension point, #187)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from django.http import Http404
from django.utils.translation import gettext


def comment_items(user: Any, spec: Any, record: Any) -> Iterable[Any]:
    from skeleton.web.timeline import LIMIT, TimelineItem

    from . import services

    if getattr(spec, "comments", None) is None:
        return []
    try:
        rows = services.comments_for(spec, record.pk, user=user)
    except Http404:
        return []
    return [
        TimelineItem(
            at=row.created_at,
            kind="comment",
            title=gettext("Comentário editado") if row.edited_at else gettext("Comentário"),
            detail=row.body,
            actor_id=row.created_by,
        )
        for row in rows.order_by("-created_at", "-sysid")[:LIMIT]
    ]
