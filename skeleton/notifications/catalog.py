"""Notification types grouped for the configuration screens (#784, #785): presentation, never a rule.

Groups come in a fixed order: the platform, the organization, then one group per app (by the
app's name). The code of a type is an identifier: only the platform's screens show it, as a
secondary line.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from django.utils.translation import gettext_lazy

from .registry import type_context

CONTEXT_LABELS = {
    "platform": gettext_lazy("Da plataforma"),
    "organization": gettext_lazy("Da organização"),
    "app": gettext_lazy("Dos aplicativos"),
}
AUDIENCE_LABELS = {
    "members": gettext_lazy("Membros"),
    "external": gettext_lazy("Pessoas externas"),
    "platform": gettext_lazy("Administradores da plataforma"),
}
CHANNEL_LABELS = {
    "in_app": gettext_lazy("Na aplicação"),
    "email": gettext_lazy("Email"),
    "push": gettext_lazy("Push no navegador"),
}


@dataclass
class Group:
    context: str
    label: Any
    app_label: str = ""
    items: list[Any] = field(default_factory=list)


def app_name(app_label: str) -> str:
    from skeleton.system.app_registry import get_app

    try:
        return str(get_app(app_label).label)
    except KeyError:
        return app_label


def group_types(items: Iterable[Any], *, kind_of=lambda item: item) -> list[Group]:
    """``items`` (types, or rows that carry one) grouped by context and, for apps, by app."""
    platform = Group("platform", CONTEXT_LABELS["platform"])
    organization = Group("organization", CONTEXT_LABELS["organization"])
    apps: dict[str, Group] = {}
    for item in items:
        kind = kind_of(item)
        context = type_context(kind)
        if context == "platform":
            platform.items.append(item)
        elif context == "app":
            label = kind.owner_app
            if label not in apps:
                apps[label] = Group("app", f"{CONTEXT_LABELS['app']}: {app_name(label)}", app_label=label)
            apps[label].items.append(item)
        else:
            organization.items.append(item)
    for group in (platform, organization, *apps.values()):
        group.items.sort(key=lambda item: str(kind_of(item).label).lower())
    ordered = sorted(apps.values(), key=lambda group: str(group.label).lower())
    return [group for group in (platform, organization, *ordered) if group.items]


def channel_list(channels: Iterable[str]) -> str:
    names = [str(CHANNEL_LABELS.get(channel, channel)) for channel in channels]
    return ", ".join(names) if names else str(gettext_lazy("Só a escolha de cada pessoa"))
