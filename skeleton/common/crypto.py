"""Encryption at rest with ordered Fernet keyrings owned by the deployment.

Each keyring is a settings list: the first key encrypts, every key decrypts, so a
new key is prepended and the old ones stay until an offline rotation rewrote the
data. Keyrings are separate (MFA, identity providers) so rotating one never
touches the other, and no key is ever derived from ``SECRET_KEY``. A missing or
invalid key is a configuration error: there is no plaintext fallback.
"""

from __future__ import annotations

from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models

IDENTITY_PROVIDER_KEYS = "IDENTITY_PROVIDER_ENCRYPTION_KEYS"
REDACTED = "[REDACTED]"


def multifernet(setting: str, *, purpose: str) -> MultiFernet:
    """The MultiFernet of an ordered keyring setting; ``purpose`` names it in errors."""
    keys = getattr(settings, setting, ())
    if not keys:
        raise ImproperlyConfigured(f"{setting} must contain a Fernet key; plaintext {purpose} is forbidden.")
    try:
        return MultiFernet([Fernet(key.encode() if isinstance(key, str) else key) for key in keys])
    except (ValueError, TypeError) as exc:
        raise ImproperlyConfigured(f"Invalid {purpose} Fernet key.") from exc


def validate_keyring(setting: str, *, purpose: str) -> None:
    """Fail at startup on a malformed key instead of at the first write."""
    if getattr(settings, setting, ()):
        multifernet(setting, purpose=purpose)


class SealedSecret:
    """An encrypted value as stored; it never shows the plaintext in ``repr``, ``str`` or errors.

    The plaintext exists only in the process, at the moment of use, through ``reveal()``.
    """

    __slots__ = ("keyring", "token")

    def __init__(self, token: str, *, keyring: str) -> None:
        self.token = token
        self.keyring = keyring

    def reveal(self) -> str:
        try:
            return multifernet(self.keyring, purpose="secret").decrypt(self.token.encode()).decode()
        except InvalidToken:
            raise ImproperlyConfigured(f"{self.keyring} cannot decrypt a stored secret; retain old keys.") from None

    def __repr__(self) -> str:
        return f"SealedSecret({REDACTED})"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SealedSecret) and other.token == self.token and other.keyring == self.keyring

    def __hash__(self) -> int:
        return hash((self.token, self.keyring))


class EncryptedTextField(models.TextField):
    """A write-only secret column: assign plaintext, read a ``SealedSecret``.

    Assigning a ``str`` encrypts it with the first key of ``keyring`` before it
    reaches the database; loading yields a ``SealedSecret``. Declare the field in
    the table's ``sensitive_fields`` so the audit trail redacts it entirely
    (check ``skeleton.E026``); the core does not detect secrets automatically.
    """

    description = "Encrypted secret"

    def __init__(self, *args: Any, keyring: str = IDENTITY_PROVIDER_KEYS, **kwargs: Any) -> None:
        self.keyring = keyring
        super().__init__(*args, **kwargs)

    def deconstruct(self) -> tuple[Any, ...]:
        name, path, args, kwargs = super().deconstruct()
        if self.keyring != IDENTITY_PROVIDER_KEYS:
            kwargs["keyring"] = self.keyring
        return name, path, args, kwargs

    def from_db_value(self, value: str | None, expression: Any, connection: Any) -> SealedSecret | None:
        return None if value is None else SealedSecret(value, keyring=self.keyring)

    def to_python(self, value: Any) -> SealedSecret | str | None:
        if value is None or isinstance(value, SealedSecret | str):
            return value
        raise TypeError("Encrypted fields accept text or a SealedSecret.")

    def get_prep_value(self, value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, SealedSecret):
            if value.keyring != self.keyring:
                raise ImproperlyConfigured("A secret cannot move between keyrings without decrypting it.")
            return value.token
        if not isinstance(value, str):
            raise TypeError("Encrypted fields accept text or a SealedSecret.")
        return multifernet(self.keyring, purpose="secret").encrypt(value.encode()).decode()

    def value_to_string(self, obj: models.Model) -> str:
        # Serialization (dumpdata) carries the ciphertext, never the plaintext.
        value = self.value_from_object(obj)
        return value.token if isinstance(value, SealedSecret) else REDACTED

    def rotate(self, value: SealedSecret) -> SealedSecret:
        """Re-encrypt a stored secret with the first key; InvalidToken means an old key is missing."""
        token = multifernet(self.keyring, purpose="secret").rotate(value.token.encode()).decode()
        return SealedSecret(token, keyring=self.keyring)


def encrypted_fields(keyring: str, *, registry: Any = None) -> list[tuple[type[models.Model], EncryptedTextField]]:
    """Every concrete model field encrypted with ``keyring``."""
    from django.apps import apps

    result = []
    for model in (registry or apps).get_models():
        for field in model._meta.concrete_fields:
            if isinstance(field, EncryptedTextField) and field.keyring == keyring:
                result.append((model, field))
    return result
