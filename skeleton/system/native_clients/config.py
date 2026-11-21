"""The operator's configuration of native client releases (#606), in a private JSON file.

``SKELETON_NATIVE_CLIENTS_CONFIG`` names a file provisioned with mode 0600, outside the
repository and the instance SQLite::

    {
      "trust_store": "trust.json",
      "clients": {
        "acme-desktop": {
          "repository": "acme/desktop",
          "token_file": "acme-github-token",
          "channels": ["stable", "beta"],
          "publisher": "acme",
          "enabled": true
        }
      }
    }

``trust_store`` is the same operator trust store as the packages (publisher -> Ed25519 public
key, ``skeleton.system.packaging.signing.load_trust_store``); relative paths resolve against
the configuration's own directory. ``token_file`` holds a GitHub token with read access to the
releases (optional for a public repository). ``enabled: false`` stops the sync and the serving
of that client; installed clients keep their current version.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

CLIENT_ID = re.compile(r"[a-z0-9][a-z0-9._-]{1,63}\Z")
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}\Z")
MAX_CONFIG_BYTES = 256 * 1024


@dataclass(frozen=True)
class ClientReleases:
    client_id: str
    repository: str
    channels: tuple[str, ...]
    publisher: str
    token_file: Path | None
    enabled: bool


@dataclass(frozen=True)
class NativeClientsConfig:
    clients: dict[str, ClientReleases]
    trust_store: dict[str, Any]

    def client(self, client_id: str) -> ClientReleases | None:
        found = self.clients.get(client_id)
        return found if found is not None and found.enabled else None


EMPTY = NativeClientsConfig(clients={}, trust_store={})


def _relative(base: Path, value: Any, name: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ImproperlyConfigured(f"Native clients config: {name} must be a path.")
    path = Path(value)
    return path if path.is_absolute() else base / path


def load_config(path: str | Path | None = None) -> NativeClientsConfig:
    """Parse and validate the file; no file configured is the empty configuration."""
    from skeleton.system.packaging.errors import PackageError
    from skeleton.system.packaging.primitives import _json
    from skeleton.system.packaging.signing import load_trust_store
    from skeleton.system.releases import GITHUB_REPOSITORY

    path = path or getattr(settings, "SKELETON_NATIVE_CLIENTS_CONFIG", "") or ""
    if not path:
        return EMPTY
    path = Path(path)
    try:
        if path.stat().st_size > MAX_CONFIG_BYTES:
            raise ImproperlyConfigured("Native clients config is too large.")
        data = _json(path.read_bytes())
    except (OSError, PackageError) as exc:
        raise ImproperlyConfigured("Native clients config cannot be read.") from exc
    if (
        not isinstance(data, dict)
        or set(data) - {"trust_store", "clients"}
        or not isinstance(data.get("clients"), dict)
    ):
        raise ImproperlyConfigured("Native clients config needs trust_store and clients only.")
    try:
        trust_store = load_trust_store(_relative(path.parent, data.get("trust_store"), "trust_store"))
    except (OSError, PackageError) as exc:
        raise ImproperlyConfigured("Native clients trust store cannot be read.") from exc
    clients = {}
    allowed = {"repository", "token_file", "channels", "publisher", "enabled"}
    for client_id, entry in data["clients"].items():
        if not isinstance(client_id, str) or not CLIENT_ID.fullmatch(client_id):
            raise ImproperlyConfigured("Native clients config: invalid client id.")
        if not isinstance(entry, dict) or set(entry) - allowed:
            raise ImproperlyConfigured(f"Native clients config: unknown fields for {client_id}.")
        repository, channels, publisher = entry.get("repository"), entry.get("channels"), entry.get("publisher")
        if not isinstance(repository, str) or not GITHUB_REPOSITORY.fullmatch(repository):
            raise ImproperlyConfigured(f"Native clients config: invalid repository for {client_id}.")
        if (
            not isinstance(channels, list)
            or not channels
            or not all(isinstance(channel, str) and NAME.fullmatch(channel) for channel in channels)
            or len(set(channels)) != len(channels)
        ):
            raise ImproperlyConfigured(f"Native clients config: invalid channels for {client_id}.")
        if not isinstance(publisher, str) or publisher not in trust_store:
            raise ImproperlyConfigured(f"Native clients config: publisher of {client_id} is not in the trust store.")
        enabled = entry.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ImproperlyConfigured(f"Native clients config: enabled of {client_id} must be a boolean.")
        token = entry.get("token_file")
        clients[client_id] = ClientReleases(
            client_id=client_id,
            repository=repository,
            channels=tuple(channels),
            publisher=publisher,
            token_file=_relative(path.parent, token, "token_file") if token is not None else None,
            enabled=enabled,
        )
    return NativeClientsConfig(clients=clients, trust_store=trust_store)
