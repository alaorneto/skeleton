"""The per-process cache of the instance appearance (#592): TTL, pin, last good value, no query."""

import pytest
from django.db import DatabaseError

from skeleton.appearance import snapshot
from skeleton.appearance.snapshot import (
    DEFAULT,
    Appearance,
    cached_appearance,
    current_appearance,
    pin_appearance,
    reset_appearance_cache,
)


@pytest.fixture
def clock(monkeypatch):
    now = {"value": 1000.0}
    monkeypatch.setattr(snapshot.time, "monotonic", lambda: now["value"])
    pin_appearance(None)
    reset_appearance_cache()
    return now


def loader(monkeypatch, *values):
    calls = []
    queue = list(values)

    def load():
        calls.append(1)
        value = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(snapshot, "_load", load)
    return calls


def test_the_value_is_read_once_per_ttl(clock, monkeypatch):
    first, second = Appearance(product_name="Um"), Appearance(product_name="Dois")
    calls = loader(monkeypatch, first, second)
    assert current_appearance() == first
    clock["value"] += snapshot.TTL_S - 0.1
    assert current_appearance() == first
    assert len(calls) == 1
    clock["value"] += 0.2
    assert current_appearance() == second
    assert len(calls) == 2


def test_reset_reads_again_at_once(clock, monkeypatch):
    calls = loader(monkeypatch, Appearance(product_name="Um"), Appearance(product_name="Dois"))
    current_appearance()
    reset_appearance_cache()
    assert current_appearance().product_name == "Dois"
    assert len(calls) == 2


def test_a_database_error_keeps_the_last_good_value_then_the_default(clock, monkeypatch):
    good = Appearance(product_name="Bom")
    loader(monkeypatch, good, DatabaseError("down"))
    assert current_appearance() == good
    clock["value"] += snapshot.TTL_S + 1
    assert current_appearance() == good
    clock["value"] += snapshot.STALE_S
    assert current_appearance() == DEFAULT


def test_without_a_good_value_a_database_error_is_the_default(clock, monkeypatch):
    loader(monkeypatch, DatabaseError("down"))
    assert current_appearance() == DEFAULT


def test_the_pin_wins_and_never_loads(clock, monkeypatch):
    calls = loader(monkeypatch, Appearance(product_name="Banco"))
    pinned = Appearance(product_name="Fixo")
    pin_appearance(pinned)
    assert current_appearance() == pinned
    assert cached_appearance() == pinned
    assert calls == []


def test_before_the_apps_are_ready_it_is_the_default(clock, monkeypatch):
    from django.apps import apps

    calls = loader(monkeypatch, Appearance(product_name="Banco"))
    monkeypatch.setattr(apps, "ready", False)
    assert current_appearance() == DEFAULT
    assert calls == []


def test_cached_appearance_only_reads_memory(clock, monkeypatch):
    calls = loader(monkeypatch, Appearance(product_name="Banco"))
    assert cached_appearance() == DEFAULT
    assert calls == []
    current_appearance()
    assert cached_appearance().product_name == "Banco"
    assert len(calls) == 1


def test_setup_mode_is_the_default_without_touching_the_database(clock, monkeypatch, settings):
    """Setup pages render through the same shell, with the dummy backend (found in the browser check)."""
    calls = loader(monkeypatch, Appearance(product_name="Banco"))
    settings.SKELETON_SETUP_MODE = True
    assert current_appearance() == DEFAULT
    assert calls == []
