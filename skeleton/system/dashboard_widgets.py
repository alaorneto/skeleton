"""Widgets of the organization dashboard declared by apps (#620), registered in code like job types.

An app registers a ``DashboardWidget`` in ``AppConfig.ready()``: a template of its own and a
``context(*, request, actor)`` callable that authorizes through the app's service API. The core
shows a widget only to whom may see it (``visible_widgets``: the owner app is effective in the
organization and the person passes the optional gate) and renders it in ``skeleton/web/widgets.py``,
where any error of the app becomes a neutral box. The gate only hides: it never grants access,
and ``context`` runs in the request's tenant context, never in ``system_context``.
Check ``skeleton.E045`` validates every registration.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ImproperlyConfigured

__all__ = [
    "DashboardWidget",
    "dashboard_widget_problems",
    "dashboard_widgets",
    "get_dashboard_widget",
    "register_dashboard_widget",
    "visible_widgets",
]

_NAME = re.compile(r"(?P<owner>[a-z][a-z0-9_]*)\.(?P<slug>[a-z0-9_]{1,40})")


@dataclass(frozen=True)
class DashboardWidget:
    """One box of the organization dashboard; ``name`` is ``"<owner_app>.<slug>"``.

    ``label`` names the box in the editor and is its default title. ``template`` is the app's own
    template; ``context(*, request, actor)`` returns its mapping and authorizes by the app's service
    API (default deny). The optional gate, like a menu entry: ``(table_key, action)`` among the
    person's table permissions, or ``capability`` among their capabilities.
    """

    name: str
    owner_app: str
    label: Any
    template: str
    context: Callable[..., Mapping[str, Any]]
    table_key: str = ""
    action: str = "read"
    capability: str = ""


_registry: dict[str, DashboardWidget] = {}


def register_dashboard_widget(widget: DashboardWidget) -> DashboardWidget:
    """Register in ``AppConfig.ready()``; a repeated name or a name outside ``owner_app`` is refused."""
    match = _NAME.fullmatch(widget.name or "")
    if match is None:
        raise ImproperlyConfigured(
            f"Dashboard widget name {widget.name!r} must be '<owner_app>.<slug>' with slug [a-z0-9_]{{1,40}}."
        )
    if match["owner"] != widget.owner_app:
        raise ImproperlyConfigured(
            f"Dashboard widget {widget.name!r} must start with its owner_app {widget.owner_app!r}."
        )
    existing = _registry.get(widget.name)
    if existing is not None:
        if existing == widget:
            return existing  # The same declaration loaded again (a second ready()); nothing changes.
        raise ImproperlyConfigured(f"Dashboard widget {widget.name!r} is already registered.")
    _registry[widget.name] = widget
    return widget


def unregister_dashboard_widget(name: str) -> None:
    """Tests only: remove a registration (restore the registry at the end of the test)."""
    _registry.pop(name, None)


def dashboard_widgets() -> tuple[DashboardWidget, ...]:
    return tuple(_registry[name] for name in sorted(_registry))


def get_dashboard_widget(name: str) -> DashboardWidget | None:
    return _registry.get(name)


def visible_widgets(user: Any) -> tuple[DashboardWidget, ...]:
    """Widgets of effective apps whose gate ``user`` passes, in a fixed number of queries.

    Requires the tenant context. The app states and the table permissions are read once each,
    whatever the number of widgets; capabilities only when a widget asks for one.
    """
    from .applications import application_states

    widgets = dashboard_widgets()
    if not widgets:
        return ()
    states = application_states()
    candidates = [widget for widget in widgets if states.get(widget.owner_app, {}).get("effective")]
    if not candidates:
        return ()
    permissions: frozenset[tuple[str, str]] = frozenset()
    capabilities: frozenset[str] = frozenset()
    if any(widget.table_key for widget in candidates):
        from skeleton.access.policies import table_permissions

        permissions = table_permissions(user)
    if any(widget.capability for widget in candidates):
        from skeleton.access.capabilities import capabilities_for

        capabilities = frozenset(capabilities_for(user))
    result = []
    for widget in candidates:
        if widget.table_key:
            owner = widget.table_key.split(".", 1)[0]
            if owner in states and not states[owner]["effective"]:
                continue
            if (widget.table_key, widget.action) not in permissions:
                continue
        if widget.capability and widget.capability not in capabilities:
            continue
        result.append(widget)
    return tuple(result)


def dashboard_widget_problems() -> list[str]:
    """What check E045 reports: unknown owner app or table, invalid action, missing template, bad context."""
    from django.apps import apps
    from django.template import TemplateDoesNotExist
    from django.template.loader import get_template

    from skeleton.access.policies import ACTIONS

    from .registry import get_spec

    problems = []
    installed = {config.label for config in apps.get_app_configs()}
    for widget in dashboard_widgets():
        if widget.owner_app not in installed:
            problems.append(f"{widget.name}: owner_app {widget.owner_app!r} is not an installed app")
        if widget.table_key:
            try:
                get_spec(widget.table_key)
            except (KeyError, LookupError):
                problems.append(f"{widget.name}: table_key {widget.table_key!r} has no registered TableSpec")
        if widget.action not in ACTIONS:
            problems.append(f"{widget.name}: action {widget.action!r} is not a table action")
        if not callable(widget.context):
            problems.append(f"{widget.name}: context is not callable")
        try:
            get_template(widget.template)
        except TemplateDoesNotExist:
            problems.append(f"{widget.name}: template {widget.template!r} does not exist")
    return problems
