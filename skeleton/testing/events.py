"""Capture the domain events published inside a block, for assertions in tests."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from skeleton.events import services

__all__ = ["capture_events"]


@contextmanager
def capture_events() -> Iterator[list]:
    """Yield a list that receives every ``DomainEvent`` published in the block, committed or not."""
    captured: list = []
    token = services._captured.set(captured)
    try:
        yield captured
    finally:
        services._captured.reset(token)
