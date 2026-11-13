"""Callbacks run when an organization is created (#396).

``skeleton.tenancy.services.create_organization`` calls them inside its transaction, in a
``system_context`` of the new organization, after the builtin roles and the owner's membership
exist. A subsystem registers one in ``AppConfig.ready`` (billing gives the organization its
default plan); a failure rolls the creation back. Pure Python, no ``skeleton.*`` import.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

_created: list[Callable[[Any], None]] = []


def register_organization_created(callback: Callable[[Any], None]) -> None:
    if callback not in _created:
        _created.append(callback)


def organization_created(organization: Any) -> None:
    for callback in tuple(_created):
        callback(organization)
