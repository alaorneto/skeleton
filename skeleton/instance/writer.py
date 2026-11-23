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
from .resolve import instance_paths, is_cluster
from .schema import SettingSpec, spec_for
from .store import InstanceStore


class InstanceClusterMode(RuntimeError):
    """In cluster mode (#673) the configuration comes from the environment: nothing writes it here."""

    code = "instance_cluster_mode"

    def __init__(self) -> None:
        super().__init__(
            "instance_cluster_mode: no modo cluster a configuração vem do ambiente, igual em todos os nós; "
            "mude o arquivo de ambiente (ou o cofre de segredos) e reinicie os nós."
        )


def refuse_in_cluster(environment: Mapping[str, str] | None = None) -> None:
    if is_cluster(environment):
        raise InstanceClusterMode


def open_store(base_dir: Path, environment: Mapping[str, str] | None = None) -> tuple[InstanceStore, Path]:
    database, key = instance_paths(base_dir, os.environ if environment is None else environment)
    return InstanceStore(database), key


def write_setting(base_dir: Path, key: str, raw: str, *, source: str) -> SettingSpec:
    """Validate ``raw`` with the catalog and store it; a secret is encrypted with the master key."""
    refuse_in_cluster()
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


def _quoted(name: str, value: str) -> str:
    """``NAME="value"`` read the same by Compose ``env_file`` and systemd ``EnvironmentFile``."""
    if any(mark in value for mark in ("\n", "\r", "\x00")):
        raise ValueError(f"export_value_multiline: {name} tem quebra de linha e não cabe num arquivo de ambiente.")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
    return f'{name}="{escaped}"'


def export_environment(base_dir: Path, output: Path, *, source: str) -> list[str]:
    """Write every effective catalog value (secrets decrypted) to a new 0600 file (#673).

    For moving a standalone instance to cluster mode: the file goes to the secret store and becomes
    the ``env_file`` of every node. Only in standalone mode, never over an existing file, never to
    the terminal. The change log records the export without any value.
    """
    from .resolve import LayeredEnviron, resolve_instance_settings

    refuse_in_cluster()
    store, key_path = open_store(base_dir)
    layered = LayeredEnviron(os.environ, store.settings(), key_path)
    lines, names = ["# Configuração da instância exportada por manage.py instance export-env (#673)."], []
    for item in resolve_instance_settings(base_dir):
        if item.origin == "default":
            continue
        lines.append(_quoted(item.env, layered[item.env]))
        names.append(item.key)
    try:
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ValueError(f"export_target_exists: {output} já existe; escolha outro arquivo.") from None
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")
    if store.exists():
        store.log_change("*", "export-env", source)
    return names


REQUIRED_KEY_MESSAGE = "Esta chave é obrigatória para a instância iniciar: grave um novo valor em vez de removê-la."


def can_clear(spec: SettingSpec, environment: Mapping[str, str] | None = None) -> bool:
    """A required key may leave the store only while the environment supplies it (#267).

    Without it the next start falls into setup mode, whose pages answer 404 once the setup
    is complete: the whole site would be unreachable until someone edits the host.
    """
    environment = os.environ if environment is None else environment
    return not spec.required_for_setup or spec.env in environment


def clear_setting(base_dir: Path, key: str, *, source: str, allow_required: bool = False) -> bool:
    refuse_in_cluster()
    spec = spec_for(key)
    if not allow_required and not can_clear(spec):
        raise ValueError(REQUIRED_KEY_MESSAGE)
    store, _ = open_store(base_dir)
    return store.delete(spec.key, source=source)
