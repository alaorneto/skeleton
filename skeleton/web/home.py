"""The configurable internal home of an organization (#624): the published blocks or the default.

Only presentation. The core blocks read with the same authorized queries as their full screens
(``skeleton.flows.inbox`` for the person's tasks, ``skeleton.notifications.inbox`` for their
notifications), always in the request's tenant context and limited to five rows. A ``widget``
block shows only a widget that ``visible_widgets`` returns to this person; a widget of an app
turned off, or whose gate the person does not pass, simply disappears, and a widget that fails
becomes the neutral box (``skeleton.web.widgets``).
"""

from __future__ import annotations

from typing import Any

from django.shortcuts import render
from django.urls import reverse

__all__ = ["HOME_ROWS", "home_blocks", "render_home"]

HOME_ROWS = 5


def home_blocks(request: Any, organization_id: Any, blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The blocks ready for ``pages/blocks.html``: data of the core blocks and the widgets' HTML."""
    from skeleton.system.dashboard_widgets import visible_widgets

    from .widgets import render_widget

    kwargs = {"organization_id": organization_id}
    widgets: dict[str, Any] | None = None
    result = []
    for block in blocks:
        kind = block.get("type")
        if kind == "tasks":
            from skeleton.flows.inbox import organization_open_tasks

            rows = organization_open_tasks(request.user, organization_id, limit=HOME_ROWS)
            result.append({**block, "rows": rows, "url": reverse("flows:tasks", kwargs=kwargs)})
        elif kind == "notifications":
            from skeleton.notifications.inbox import unread_rows

            rows = unread_rows(request.user, limit=HOME_ROWS)
            result.append({**block, "rows": rows, "url": reverse("notifications:organization", kwargs=kwargs)})
        elif kind == "widget":
            if widgets is None:
                widgets = {widget.name: widget for widget in visible_widgets(request.user)}
            widget = widgets.get(block.get("name", ""))
            if widget is None:
                continue  # App off or gate not passed: the box disappears, the page stays valid.
            result.append({**block, "label": widget.label, "html": render_widget(request, widget)})
        else:
            result.append(block)
    return result


def render_home(request: Any, organization_id: Any, document: dict[str, Any], **extra: Any):
    """The internal home with ``document`` (the published page, the default or a preview)."""
    from skeleton.service_api.organizations import OrganizationService

    organization = OrganizationService().current(actor=request.user)
    return render(
        request,
        "skeleton/web/dashboard.html",
        {
            "organization": organization,
            "organization_id": organization_id,
            "blocks": home_blocks(request, organization_id, list(document.get("blocks", []))),
            **extra,
        },
    )
