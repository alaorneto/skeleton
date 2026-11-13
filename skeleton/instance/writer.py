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
        if not key_path.is_file() and any(secret for _, secret in store.settings().values()):
            raise crypto.MasterKeyMissing(
                f"Chave mestra ausente em {key_path} e há segredos gravados com ela: restaure o arquivo de backup "
                "antes de gravar outro segredo."
            )
        value = crypto.encrypt(crypto.load_key(key_path, create=True), value)
    store.put(spec.key, value, secret=spec.secret, source=source)
    return spec


REQUIRED_KEY_MESSAGE = "Esta chave é obrigatória para a instância iniciar: grave um novo valor em vez de removê-la."


def can_clear(spec: SettingSpec, environment: Mapping[str, str] | None = None) -> bool:
    """A required key may leave the store only while the environment supplies it (#267).

    Without it the next start falls into setup mode, whose pages answer 404 once the setup
    is complete: the whole site would be unreachable until someone edits the host.
    """
    environment = os.environ if environment is None else environment
    return not spec.required_for_setup or spec.env in environment


def clear_setting(base_dir: Path, key: str, *, source: str, allow_required: bool = False) -> bool:
    spec = spec_for(key)
    if not allow_required and not can_clear(spec):
        raise ValueError(REQUIRED_KEY_MESSAGE)
    store, _ = open_store(base_dir)
    return store.delete(spec.key, source=source)
