"""The instance SQLite: configuration and setup state only, never application data.

Plain ``sqlite3`` from the standard library, not a ``DATABASES`` alias and without Django
migrations: the settings read it before Django exists. Secrets are stored as Fernet tokens;
the change log records which key changed and from where, never the value.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 1
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS setting ("
    " key TEXT PRIMARY KEY, value TEXT NOT NULL, secret INTEGER NOT NULL CHECK (secret IN (0, 1)),"
    " updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS setup_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS setup_token ("
    " token_hash TEXT PRIMARY KEY, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, used_at TEXT)",
    "CREATE TABLE IF NOT EXISTS change_log ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, action TEXT NOT NULL,"
    " source TEXT NOT NULL, changed_at TEXT NOT NULL)",
)


class InstanceStoreError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


class InstanceStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def exists(self) -> bool:
        return self.path.is_file()

    def _connect(self, *, create: bool) -> sqlite3.Connection:
        if not self.exists():
            if not create:
                raise InstanceStoreError(f"Armazém da instância ausente em {self.path}.")
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
            except FileExistsError:
                pass
        connection = sqlite3.connect(self.path, isolation_level=None)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        with closing(self._connect(create=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            for statement in _SCHEMA:
                connection.execute(statement)
            if connection.execute("SELECT count(*) FROM schema_version").fetchone()[0] == 0:
                connection.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
            connection.execute("COMMIT")

    def schema_version(self) -> int | None:
        if not self.exists():
            return None
        with closing(self._connect(create=False)) as connection:
            try:
                row = connection.execute("SELECT max(version) FROM schema_version").fetchone()
            except sqlite3.OperationalError:
                return None
        return row[0]

    def settings(self) -> dict[str, tuple[str, bool]]:
        """Stored ``key -> (value, secret)``; secret values are still encrypted tokens."""
        if not self.exists():
            return {}
        with closing(self._connect(create=False)) as connection:
            try:
                rows = connection.execute("SELECT key, value, secret FROM setting").fetchall()
            except sqlite3.OperationalError as exc:
                raise InstanceStoreError(f"Armazém da instância ilegível em {self.path}.") from exc
        return {key: (value, bool(secret)) for key, value, secret in rows}

    def put(self, key: str, value: str, *, secret: bool, source: str) -> None:
        self.initialize()
        with closing(self._connect(create=False)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existed = connection.execute("SELECT 1 FROM setting WHERE key = ?", (key,)).fetchone()
            connection.execute(
                "INSERT INTO setting (key, value, secret, updated_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value, secret = excluded.secret,"
                " updated_at = excluded.updated_at",
                (key, value, int(secret), _now()),
            )
            connection.execute(
                "INSERT INTO change_log (key, action, source, changed_at) VALUES (?, ?, ?, ?)",
                (key, "update" if existed else "create", source, _now()),
            )
            connection.execute("COMMIT")

    def delete(self, key: str, *, source: str) -> bool:
        if not self.exists():
            return False
        with closing(self._connect(create=False)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            removed = connection.execute("DELETE FROM setting WHERE key = ?", (key,)).rowcount
            if removed:
                connection.execute(
                    "INSERT INTO change_log (key, action, source, changed_at) VALUES (?, ?, ?, ?)",
                    (key, "delete", source, _now()),
                )
            connection.execute("COMMIT")
        return bool(removed)

    def changes(self) -> list[tuple[str, str, str, str]]:
        if not self.exists():
            return []
        with closing(self._connect(create=False)) as connection:
            return connection.execute("SELECT key, action, source, changed_at FROM change_log ORDER BY id").fetchall()

    def setup_state(self) -> dict[str, str]:
        if not self.exists():
            return {}
        with closing(self._connect(create=False)) as connection:
            try:
                return dict(connection.execute("SELECT key, value FROM setup_state").fetchall())
            except sqlite3.OperationalError:
                return {}
