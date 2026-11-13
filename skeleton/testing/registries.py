"""Isolate the core registries in a test, and put every original object back on the way out.

The registries are module state filled in ``AppConfig.ready()``. A test that declares its own
job, event or notification types, token scopes or a different ``TableSpec`` uses one of these
context managers, so nothing it registers reaches the next test, even when it fails:

```python
@pytest.fixture(autouse=True)
def declarations():
    with isolated_job_types():
        register_job_type(JobType("tasks.remind", remind, {"task_id": "str"}))
        yield
```

Only for tests; production code never imports this module.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from contextlib import contextmanager
from types import ModuleType
from typing import Any

__all__ = [
    "isolated_event_types",
    "isolated_job_types",
    "isolated_notification_types",
    "isolated_token_scopes",
    "table_spec_override",
]


@contextmanager
def _swapped(replacements: list[tuple[ModuleType, str, Any]]) -> Iterator[None]:
    originals = [(module, name, getattr(module, name)) for module, name, _ in replacements]
    try:
        for module, name, value in replacements:
            setattr(module, name, value)
        yield
    finally:
        for module, name, value in originals:
            setattr(module, name, value)


@contextmanager
def isolated_job_types(*, keep_existing: bool = False) -> Iterator[None]:
    """Job types and periodic jobs start empty (or as copies with ``keep_existing``)."""
    from skeleton.jobs import periodic, registry

    with _swapped(
        [
            (registry, "_registry", dict(registry._registry) if keep_existing else {}),
            (registry, "_duplicates", list(registry._duplicates) if keep_existing else []),
            (periodic, "_periodic", dict(periodic._periodic) if keep_existing else {}),
        ]
    ):
        yield


@contextmanager
def isolated_event_types(*, keep_core: bool = True, keep_consumers: bool = False) -> Iterator[None]:
    """Event types start as a copy of the registered ones (the core types, #43); consumers start empty.

    ``keep_consumers`` starts the consumers as a copy instead, for a test that relies on the
    consumers the core registers (webhooks, flows) and only adds event types.
    """
    from skeleton.events import registry

    with _swapped(
        [
            (registry, "_types", dict(registry._types) if keep_core else {}),
            (registry, "_consumers", dict(registry._consumers) if keep_consumers else {}),
            (registry, "_duplicates", []),
        ]
    ):
        yield


@contextmanager
def isolated_notification_types() -> Iterator[None]:
    """Notification types start empty."""
    from skeleton.notifications import registry

    with _swapped([(registry, "_types", {}), (registry, "_duplicates", [])]):
        yield


@contextmanager
def isolated_token_scopes() -> Iterator[None]:
    """Named token scopes start as a copy of the registered ones."""
    from skeleton.integrations.tokens import scopes

    with _swapped([(scopes, "_named", dict(scopes._named))]):
        yield


@contextmanager
def table_spec_override(table_key: str, **changes: Any) -> Iterator[Any]:
    """Replace the ``TableSpec`` of ``table_key`` by a copy with ``changes``; yields the copy."""
    from skeleton.system import registry

    original = registry._registry[table_key]
    changed = dataclasses.replace(original, **changes)
    registry._registry[table_key] = changed
    try:
        yield changed
    finally:
        registry._registry[table_key] = original
