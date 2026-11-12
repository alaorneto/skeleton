"""Notification types are registered in code by the app that owns them."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class NotificationType:
    """A kind of notification.

    ``payload_fields`` are the scalar keys ``notify()`` accepts; ``snapshot_fields``
    are copied from the target record and must be ``read_fields`` and never
    ``sensitive_fields`` of ``target_table_key``. ``group_key`` is a format string
    over the payload and ``target`` (for example ``"task:{target}"``): unread
    notifications of a person with the same key collapse into one line.
    """

    code: str
    label: str
    payload_fields: tuple[str, ...] = ()
    snapshot_fields: tuple[str, ...] = ()
    target_table_key: str = ""
    default_channels: tuple[str, ...] = ("in_app",)
    mandatory: bool = False
    templates: Mapping[str, str] = field(default_factory=dict)
    group_key: str = ""
    owner_app: str = ""
    # Web push carries only the type label and a link; True also sends the snapshot (#56).
    push_snapshot: bool = False
    # For a type without a target table: ``open_url(user, row) -> str | None`` re-authorizes the
    # person and returns where the notification leads (the flow task page, #204).
    open_url: Callable[[Any, Any], str | None] | None = None
    # For a type without a target table: ``authorize(user, notification) -> bool`` re-checks
    # each person at fan-out time, so access revoked since the notification was created
    # stops the delivery (comment mentions, #188).
    authorize: Callable[[Any, Any], bool] | None = None


_types: dict[str, NotificationType] = {}
_duplicates: list[str] = []


def register_notification_type(notification_type: NotificationType) -> NotificationType:
    existing = _types.get(notification_type.code)
    if existing is not None and existing != notification_type:
        _duplicates.append(notification_type.code)
        return existing
    _types[notification_type.code] = notification_type
    return notification_type


def get_notification_type(code: str) -> NotificationType:
    return _types[code]


def all_notification_types() -> tuple[NotificationType, ...]:
    return tuple(_types[code] for code in sorted(_types))


def notification_problems() -> list[str]:
    from skeleton.system.registry import get_spec

    problems = [f"{code}: duplicate notification type" for code in sorted(set(_duplicates))]
    for kind in all_notification_types():
        if not kind.target_table_key:
            if kind.snapshot_fields:
                problems.append(f"{kind.code}: snapshot fields need a target table")
            continue
        try:
            spec = get_spec(kind.target_table_key)
        except KeyError:
            problems.append(f"{kind.code}: unknown target table {kind.target_table_key}")
            continue
        for name in kind.snapshot_fields:
            if name in spec.sensitive_fields:
                problems.append(f"{kind.code}: snapshot field {name} is sensitive")
            elif name not in spec.read_fields:
                problems.append(f"{kind.code}: snapshot field {name} is not a read field")
        for name in kind.payload_fields:
            if name in spec.sensitive_fields:
                problems.append(f"{kind.code}: payload field {name} is sensitive")
    return problems
