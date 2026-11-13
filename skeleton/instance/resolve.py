"""Resolve instance settings with the precedence environment > instance SQLite > default.

``instance_env()`` returns a ``django-environ`` ``Env`` whose variable mapping layers the
process environment (including a ``.env`` already read into it) over the store, but only for
catalog keys: every other name keeps reading the environment alone. The settings modules use
it exactly like ``environ.Env()``. ``resolve_instance_settings()`` reports the effective origin
of each catalog key, never a secret value.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import environ  # type: ignore[import-untyped]
from cryptography.fernet import MultiFernet

from . import crypto
from .schema import BY_ENV, CATALOG
from .store import InstanceStore

ORIGIN_ENV, ORIGIN_INSTANCE, ORIGIN_DEFAULT = "env", "instance", "default"


def instance_paths(base_dir: Path, environment: Mapping[str, str] | None = None) -> tuple[Path, Path]:
    """Store and master key paths; both come only from bootstrap variables."""
    environment = os.environ if environment is None else environment
    root = Path(base_dir) / "var" / "instance"
    database = Path(environment.get("SKELETON_INSTANCE_DB") or root / "instance.sqlite3")
    key = Path(environment.get("SKELETON_INSTANCE_KEY_FILE") or root / "master.key")
    if database.resolve() == key.resolve():
        raise crypto.InstanceKeyError("A chave mestra precisa ficar num arquivo separado do SQLite da instância.")
    return database, key


class LayeredEnviron(Mapping[str, str]):
    """Process environment first; catalog keys fall back to the store, decrypted on first read."""

    def __init__(self, environment: Mapping[str, str], stored: dict[str, tuple[str, bool]], key_path: Path):
        self._environment = environment
        self._key_path = key_path
        self._fernet: MultiFernet | None = None
        # Stored rows by environment variable name; unknown keys (written by a newer release) are ignored.
        self._stored = {CATALOG[key].env: row for key, row in stored.items() if key in CATALOG}

    def _decrypt(self, token: str) -> str:
        if self._fernet is None:
            try:
                self._fernet = crypto.load_key(self._key_path)
            except crypto.InstanceKeyError as exc:
                from django.core.exceptions import ImproperlyConfigured

                raise ImproperlyConfigured(str(exc)) from exc
        try:
            return crypto.decrypt(self._fernet, token)
        except crypto.InstanceKeyError as exc:
            from django.core.exceptions import ImproperlyConfigured

            raise ImproperlyConfigured(str(exc)) from exc

    def __getitem__(self, name: str) -> str:
        if name in self._environment:
            return self._environment[name]
        if name not in self._stored:
            raise KeyError(name)
        value, secret = self._stored[name]
        return self._decrypt(value) if secret else value

    def __contains__(self, name: object) -> bool:
        return name in self._environment or name in self._stored

    def __iter__(self) -> Iterator[str]:
        yield from self._environment
        yield from (name for name in self._stored if name not in self._environment)

    def __len__(self) -> int:
        return len(set(self._environment) | set(self._stored))


class InstanceEnv(environ.Env):
    """``environ.Env`` reading a ``LayeredEnviron``; ``read_env`` still writes the process environment."""

    def __init__(self, layered: LayeredEnviron, **scheme: object) -> None:
        super().__init__(**scheme)
        self.ENVIRON = layered


def _store_for(base_dir: Path, environment: Mapping[str, str]) -> tuple[InstanceStore, Path]:
    database, key = instance_paths(base_dir, environment)
    return InstanceStore(database), key


def instance_env(base_dir: Path) -> InstanceEnv:
    store, key = _store_for(base_dir, os.environ)
    return InstanceEnv(LayeredEnviron(os.environ, store.settings(), key))


@dataclass(frozen=True)
class ResolvedSetting:
    key: str
    env: str
    origin: str
    secret: bool
    value: str | None  # None for secrets and for defaults defined in the settings modules.


def resolve_instance_settings(base_dir: Path, environment: Mapping[str, str] | None = None) -> list[ResolvedSetting]:
    """Effective origin of every catalog key; secret values are never returned."""
    environment = os.environ if environment is None else environment
    store, _ = _store_for(base_dir, environment)
    stored = store.settings()
    result = []
    for key, spec in CATALOG.items():
        if spec.env in environment:
            origin, raw = ORIGIN_ENV, environment[spec.env]
        elif key in stored:
            origin, raw = ORIGIN_INSTANCE, stored[key][0]
        else:
            origin, raw = ORIGIN_DEFAULT, None
        result.append(ResolvedSetting(key, spec.env, origin, spec.secret, None if spec.secret or raw is None else raw))
    return result


def missing_for_setup(base_dir: Path, environment: Mapping[str, str] | None = None) -> list[str]:
    return [
        item.key
        for item in resolve_instance_settings(base_dir, environment)
        if CATALOG[item.key].required_for_setup and item.origin == ORIGIN_DEFAULT
    ]


__all__ = [
    "BY_ENV",
    "InstanceEnv",
    "LayeredEnviron",
    "ResolvedSetting",
    "instance_env",
    "instance_paths",
    "missing_for_setup",
    "resolve_instance_settings",
]
