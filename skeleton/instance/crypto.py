"""Fernet encryption of instance secrets with a master key kept outside the SQLite file."""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class InstanceKeyError(RuntimeError):
    """The master key is missing, unreadable or does not match the stored secrets."""


def _private_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)


def load_key(path: Path, *, create: bool = False) -> Fernet:
    """Load the master key; ``create`` generates it (0600) only when it does not exist yet."""
    if not path.is_file():
        if not create:
            raise InstanceKeyError(
                f"Chave mestra da instância ausente em {path}: os segredos não podem ser lidos. "
                "Restaure o arquivo de backup ou defina SKELETON_INSTANCE_KEY_FILE."
            )
        _private_dir(path)
        key = Fernet.generate_key()
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass  # A concurrent first use won the race; read its key below.
        else:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(key)
    try:
        return Fernet(path.read_bytes().strip())
    except (OSError, ValueError) as exc:
        raise InstanceKeyError(f"Chave mestra da instância inválida em {path}.") from exc


def encrypt(fernet: Fernet, value: str) -> str:
    return fernet.encrypt(value.encode()).decode()


def decrypt(fernet: Fernet, token: str) -> str:
    try:
        return fernet.decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise InstanceKeyError("A chave mestra não decifra os segredos desta instância.") from exc
