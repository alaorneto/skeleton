"""Canonical bytes and SHA-256 digest of a seal payload (#527). Standard library only (rule R1).

The digest is what a digital signature app signs: the same payload always yields the same
bytes, whatever the order of the keys. ``canonical_bytes`` accepts JSON types plus ``datetime``
(aware, written in UTC ISO 8601), ``date`` (ISO), ``UUID`` and ``Decimal`` (as text); ``float``
is refused, because its text form is not stable across producers, and so is any other type.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from decimal import Decimal
from typing import Any

__all__ = ["canonical_bytes", "digest"]


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, bool | str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise TypeError("Seal payloads refuse float; use int, Decimal or text.")
    if isinstance(value, dt.datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise TypeError("Seal payloads need aware datetimes.")
        return value.astimezone(dt.UTC).isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, uuid.UUID | Decimal):
        return str(value)
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("Seal payload keys must be text.")
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    raise TypeError(f"Seal payloads cannot hold {type(value).__name__}.")


def canonical_bytes(payload: Any) -> bytes:
    """JSON with sorted keys and compact separators, in UTF-8."""
    return json.dumps(_plain(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def digest(payload: Any) -> str:
    """SHA-256 of ``canonical_bytes(payload)`` in lowercase hexadecimal."""
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()
