"""Administrative maintenance mode (#446): its state in the instance SQLite and a cached reader.

The state lives in the instance store, not in PostgreSQL. It is read before the session and the
database, and the CLI can leave the mode with the database down. It is independent of the
deployment marker (``SKELETON_MAINTENANCE_MARKER``), which only the deployment agent writes and
removes. A scheduled end (``ends_at``) is evaluated on read: no process writes when it passes.

Every process keeps one ``MaintenanceReader`` per store path. It costs one ``os.stat`` per call
and rereads the row when the file changes, or at most every ``MAX_AGE_S`` seconds. A store that
exists but cannot be read keeps the last state read; before any successful read it counts as
active (fail closed).
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .store import InstanceStore, InstanceStoreError

MESSAGE_MAX_LENGTH = 500
MAX_AGE_S = 5.0
RETRY_AFTER_DEFAULT_S = 300
RETRY_AFTER_MIN_S = 30

logger = logging.getLogger("skeleton.instance.maintenance")


class MaintenanceError(ValueError):
    """Invalid input for entering the maintenance mode."""


@dataclass(frozen=True)
class MaintenanceState:
    active: bool = False
    message: str = ""
    ends_at: datetime | None = None
    pause_jobs: bool = False
    started_at: datetime | None = None
    source: str = ""
    unreadable: bool = False

    def is_active(self, now: datetime | None = None) -> bool:
        if not self.active:
            return False
        return self.ends_at is None or _utc(now) < self.ends_at

    def expired(self, now: datetime | None = None) -> bool:
        return self.active and not self.is_active(now)

    def retry_after(self, now: datetime | None = None) -> int:
        if self.ends_at is None:
            return RETRY_AFTER_DEFAULT_S
        return max(RETRY_AFTER_MIN_S, math.ceil((self.ends_at - _utc(now)).total_seconds()))


INACTIVE = MaintenanceState()
# Fail closed: a store that exists but was never read successfully by this process.
UNREADABLE = MaintenanceState(active=True, pause_jobs=True, unreadable=True)


def _utc(value: datetime | None) -> datetime:
    return datetime.now(UTC) if value is None else value.astimezone(UTC)


def _parse(value: object) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def clean_message(value: str | None) -> str:
    """Plain text up to ``MESSAGE_MAX_LENGTH``; line breaks are kept, other control and format characters refused."""
    text = (value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) > MESSAGE_MAX_LENGTH:
        raise MaintenanceError(f"A mensagem tem mais de {MESSAGE_MAX_LENGTH} caracteres.")
    # Cc covers control characters, Cf the invisible format ones (bidirectional overrides included).
    if any(char != "\n" and unicodedata.category(char) in {"Cc", "Cf"} for char in text):
        raise MaintenanceError("A mensagem tem caracteres de controle.")
    return text


def clean_ends_at(value: datetime | None, *, now: datetime | None = None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise MaintenanceError("O término precisa de fuso horário.")
    value = value.astimezone(UTC)
    if value <= _utc(now):
        raise MaintenanceError("O término precisa estar no futuro.")
    return value


def read_state(store: InstanceStore) -> MaintenanceState:
    """The stored state; raises ``InstanceStoreError`` when the store exists but cannot be read."""
    row = store.maintenance()
    if row is None:
        return INACTIVE
    try:
        return MaintenanceState(
            active=bool(row["active"]),
            message=str(row["message"] or ""),
            ends_at=_parse(row["ends_at"]),
            pause_jobs=bool(row["pause_jobs"]),
            started_at=_parse(row["started_at"]),
            source=str(row["source"] or ""),
        )
    except (TypeError, ValueError) as exc:
        raise InstanceStoreError(f"Estado de manutenção ilegível em {store.path}.") from exc


def enter(
    store: InstanceStore,
    *,
    message: str | None,
    ends_at: datetime | None,
    pause_jobs: bool,
    source: str,
    now: datetime | None = None,
) -> MaintenanceState:
    """Enter the mode, or change the message or the end of an ongoing one."""
    text = clean_message(message)
    end = clean_ends_at(ends_at, now=now)
    store.put_maintenance(
        active=True, message=text, ends_at=end.isoformat() if end else None, pause_jobs=pause_jobs, source=source
    )
    return read_state(store)


def leave(store: InstanceStore, *, source: str) -> bool:
    """Leave the mode; True when it was stored as active (an expired end counts as active here)."""
    if not store.exists():
        return False
    was_active = read_state(store).active
    if was_active:
        store.put_maintenance(active=False, message="", ends_at=None, pause_jobs=False, source=source)
    return was_active


class MaintenanceReader:
    def __init__(self, path: Path | str, *, max_age_s: float = MAX_AGE_S, clock: Callable[[], float] = time.monotonic):
        self.store = InstanceStore(Path(path))
        self.max_age_s = max_age_s
        self.clock = clock
        self._lock = threading.Lock()
        self._cached: tuple[tuple[int, int] | None, float, MaintenanceState] | None = None

    def _signature(self) -> tuple[int, int] | None:
        try:
            status = os.stat(self.store.path)
        except FileNotFoundError:
            return None
        return status.st_mtime_ns, status.st_size

    def state(self) -> MaintenanceState:
        try:
            signature = self._signature()
        except OSError:
            signature = (-1, -1)  # Unknown: a read attempt then fails closed or keeps the last state.
        now = self.clock()
        cached = self._cached
        if cached is not None and cached[0] == signature and now - cached[1] < self.max_age_s:
            return cached[2]
        with self._lock:
            if signature is None:
                state = INACTIVE
            else:
                try:
                    state = read_state(self.store)
                except (InstanceStoreError, OSError) as exc:
                    previous = self._cached[2] if self._cached is not None else UNREADABLE
                    logger.error(
                        "maintenance.state_unreadable",
                        extra={"event": "maintenance.state_unreadable", "error_class": type(exc).__name__},
                    )
                    # Retried when the file changes or the cache ages, like a successful read (no log flood).
                    self._cached = (signature, now, previous)
                    return previous
            self._cached = (signature, now, state)
            return state


_readers: dict[str, MaintenanceReader] = {}
_readers_lock = threading.Lock()


def reader_for(path: Path | str) -> MaintenanceReader:
    key = os.fspath(path)
    with _readers_lock:
        reader = _readers.get(key)
        if reader is None:
            reader = _readers[key] = MaintenanceReader(key)
        return reader


def store_path() -> Path | None:
    from django.conf import settings

    path = getattr(settings, "SKELETON_MAINTENANCE_STORE", None)
    return Path(path) if path else None


def current_state() -> MaintenanceState:
    """The administrative maintenance state of this instance, as seen by this process now."""
    path = store_path()
    return reader_for(path).state() if path else INACTIVE
