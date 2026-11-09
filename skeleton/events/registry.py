"""Event types and their consumers are declared in code and validated against the TableSpec."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

WILDCARD = "*"


@dataclass(frozen=True)
class EventType:
    """``payload_fields`` must be ``read_fields`` of the table and never ``sensitive_fields``."""

    code: str
    table_key: str
    payload_fields: tuple[str, ...] = ()
    webhook: bool = False
    version: int = 1
    owner_app: str = ""


@dataclass(frozen=True)
class Consumer:
    """A reaction to events; ``handler(envelope)`` re-authorizes whatever it reads."""

    event_code: str
    code: str
    handler: Callable[..., Any]


_types: dict[str, EventType] = {}
_consumers: dict[str, Consumer] = {}
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


def register_consumer(event_code: str, code: str, handler: Callable[..., Any]) -> Consumer:
    """Subscribe ``handler`` to one event code or to ``"*"``; one delivery job per event and consumer."""
    consumer = Consumer(event_code, code, handler)
    existing = _consumers.get(code)
    if existing is not None and existing != consumer:
        _duplicates.append(f"consumer {code}")
        return existing
    _consumers[code] = consumer
    return consumer


def get_consumer(code: str) -> Consumer:
    return _consumers[code]


def consumers_for(event_code: str) -> tuple[Consumer, ...]:
    return tuple(
        _consumers[code] for code in sorted(_consumers) if _consumers[code].event_code in (event_code, WILDCARD)
    )


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
    for consumer in (_consumers[code] for code in sorted(_consumers)):
        if consumer.event_code != WILDCARD and consumer.event_code not in _types:
            problems.append(f"consumer {consumer.code}: unknown event {consumer.event_code}")
        if not callable(consumer.handler):
            problems.append(f"consumer {consumer.code}: handler is not callable")
    return problems
