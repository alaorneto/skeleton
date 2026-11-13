"""Fernet encryption of instance secrets with a master key kept outside the SQLite file.

The key file holds one key per line, the current one first; older lines still decrypt, which
lets ``manage.py instance rotate-key`` re-encrypt the store without a moment of unreadable
secrets (#342). A file with a single line is the original format.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class InstanceKeyError(RuntimeError):
    """The master key is missing, unreadable or does not match the stored secrets."""


class MasterKeyMissing(InstanceKeyError, ValueError):
    """Secrets are stored but their master key file is gone: creating another key would strand them.

    Also a ``ValueError`` so the write surfaces show it as a validation error (#323).
    """


def _private_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)


def _create_key(path: Path) -> None:
    """Publish a new key atomically: the file never exists without its content (#323)."""
    _private_dir(path)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(Fernet.generate_key())
        try:
            os.link(temporary, path)  # Fails if another process published its key first.
        except FileExistsError:
            pass
        except (AttributeError, OSError) as exc:
            raise InstanceKeyError(f"Não foi possível publicar a chave mestra em {path}.") from exc
    finally:
        os.unlink(temporary)


def read_keys(path: Path) -> list[bytes]:
    """The keys in the file, first one current; blank lines are ignored."""
    return [line.strip() for line in path.read_bytes().splitlines() if line.strip()]


def write_keys(path: Path, keys: list[bytes]) -> None:
    """Replace the key file atomically (0600): readers see the old or the new content, never a partial one."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(b"\n".join(keys) + b"\n")
        os.replace(temporary, path)
    except BaseException:
        if temporary.exists():
            temporary.unlink()
        raise


def load_key(path: Path, *, create: bool = False) -> MultiFernet:
    """Load the master key; ``create`` generates it (0600) only when it does not exist yet."""
    if not path.is_file():
        if not create:
            raise InstanceKeyError(
                f"Chave mestra da instância ausente em {path}: os segredos não podem ser lidos. "
                "Restaure o arquivo de backup ou defina SKELETON_INSTANCE_KEY_FILE."
            )
        _create_key(path)
    try:
        keys = read_keys(path)
        if not keys:
            raise ValueError("empty key file")
        return MultiFernet([Fernet(key) for key in keys])
    except (OSError, ValueError) as exc:
        raise InstanceKeyError(f"Chave mestra da instância inválida em {path}.") from exc


def encrypt(fernet: MultiFernet, value: str) -> str:
    return fernet.encrypt(value.encode()).decode()


def decrypt(fernet: MultiFernet, token: str) -> str:
    try:
        return fernet.decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise InstanceKeyError("A chave mestra não decifra os segredos desta instância.") from exc
