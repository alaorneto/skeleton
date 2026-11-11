"""Write catalog keys to the instance SQLite, validated and with secrets encrypted.

Shared by ``manage.py instance set``, the first-access setup (#146) and
``/platform/instance/`` (#147). The change log records the key, the action and the source,
never the value.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from . import crypto
from .resolve import instance_paths
from .schema import SettingSpec, spec_for
from .store import InstanceStore


def open_store(base_dir: Path, environment: Mapping[str, str] | None = None) -> tuple[InstanceStore, Path]:
    database, key = instance_paths(base_dir, os.environ if environment is None else environment)
    return InstanceStore(database), key


def write_setting(base_dir: Path, key: str, raw: str, *, source: str) -> SettingSpec:
    """Validate ``raw`` with the catalog and store it; a secret is encrypted with the master key."""
    spec = spec_for(key)
    value = spec.clean(raw)
    store, key_path = open_store(base_dir)
    if spec.secret:
        value = crypto.encrypt(crypto.load_key(key_path, create=True), value)
    store.put(spec.key, value, secret=spec.secret, source=source)
    return spec


def clear_setting(base_dir: Path, key: str, *, source: str) -> bool:
    spec = spec_for(key)
    store, _ = open_store(base_dir)
    return store.delete(spec.key, source=source)
