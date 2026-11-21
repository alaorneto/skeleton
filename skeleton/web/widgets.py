"""Rendering of the dashboard widgets that apps register (#620).

Only widgets that ``visible_widgets`` returned reach here. ``context`` runs in the request's
tenant context, inside a savepoint: whatever the app raises becomes the neutral box "Este quadro
não está disponível agora.", never a 500 and never a detail. An unexpected error is logged with
``log_internal_error`` (class and sanitized stack only); a refusal of the app's service API
(not found, forbidden, unavailable) is not an internal error and is not logged.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.template.loader import render_to_string
from django.utils.safestring import SafeString

from skeleton.common.logging import log_internal_error
from skeleton.service_api import errors as service_errors
from skeleton.system.dashboard_widgets import DashboardWidget
from skeleton.tenancy.context import current_context

__all__ = ["UNAVAILABLE_TEMPLATE", "render_widget"]

UNAVAILABLE_TEMPLATE = "skeleton/web/pages/widget-unavailable.html"
REFUSALS = (PermissionDenied, service_errors.NotFound, service_errors.Forbidden, service_errors.Unavailable)


def render_widget(request: Any, widget: DashboardWidget) -> SafeString:
    """The HTML of ``widget.template`` with its ``context``, or the neutral box when anything fails.

    The template gets the app's mapping plus ``organization_id`` and ``widget``; no context
    processor runs (the page around it already has the shell).
    """
    organization_id = current_context().organization_id
    try:
        with transaction.atomic():  # A failed query of the app never breaks the page's transaction.
            values = dict(widget.context(request=request, actor=request.user))
            html = render_to_string(widget.template, {**values, "organization_id": organization_id, "widget": widget})
    except REFUSALS:
        html = None
    except Exception as error:  # noqa: BLE001 - an app's widget never takes the dashboard down
        log_internal_error("dashboard.widget_failed", error, source="web:dashboard", reason_code=widget.name)
        html = None
    if html is None:
        html = render_to_string(UNAVAILABLE_TEMPLATE, {"widget": widget})
    return SafeString(html)
