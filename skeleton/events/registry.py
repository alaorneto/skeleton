"""Event types are declared in code and validated against the TableSpec."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EventType:
    """``payload_fields`` must be ``read_fields`` of the table and never ``sensitive_fields``."""

    code: str
    table_key: str
    payload_fields: tuple[str, ...] = ()
    webhook: bool = False
    version: int = 1
    owner_app: str = ""


_types: dict[str, EventType] = {}
_duplicates: list[str] = []


def register_event_type(event_type: EventType) -> EventType:
    existing = _types.get(event_type.code)
    if existing is not None and existing != event_type:
        _duplicates.append(event_type.code)
        return existing
    _types[event_type.code] = event_type
    return event_type


def get_event_type(code: str) -> EventType:
    return _types[code]


def all_event_types() -> tuple[EventType, ...]:
    return tuple(_types[code] for code in sorted(_types))


def event_problems() -> list[str]:
    from skeleton.system.registry import get_spec

    problems = [f"{code}: duplicate registration" for code in sorted(set(_duplicates))]
    for event_type in all_event_types():
        try:
            spec = get_spec(event_type.table_key)
        except KeyError:
            problems.append(f"{event_type.code}: unknown table {event_type.table_key}")
            continue
        for name in event_type.payload_fields:
            if name in spec.sensitive_fields:
                problems.append(f"{event_type.code}: payload field {name} is sensitive")
            elif name not in spec.read_fields:
                problems.append(f"{event_type.code}: payload field {name} is not a read field")
        if event_type.version < 1:
            problems.append(f"{event_type.code}: version must be positive")
    return problems
