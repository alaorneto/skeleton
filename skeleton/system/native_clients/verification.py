"""Verification of a native client release (#606): the signed envelope and the Velopack feed.

The envelope ``releases.<os>.sig.json`` is::

    {"payload": {"client_id", "channel", "feed_sha256", "signed_at", "expires_at"},
     "signature": "<base64 Ed25519>"}

The signature covers the canonical JSON of ``payload`` (``canonical_json``), verified with the
publisher's key from the operator's trust store, the same primitives as the packages. Every
refusal is a ``ReleaseRefused`` with a stable code, recorded by the sync without the content.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,199}\Z")
SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
MAX_ASSETS = 500
PAYLOAD_FIELDS = frozenset({"client_id", "channel", "feed_sha256", "signed_at", "expires_at"})


class ReleaseRefused(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Envelope:
    client_id: str
    channel: str
    feed_sha256: str
    signed_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class FeedFile:
    name: str
    sha256: str
    size: int


@dataclass(frozen=True)
class Feed:
    version: str
    files: tuple[FeedFile, ...]


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_envelope(
    data: bytes, *, feed: bytes, client_id: str, channels: tuple[str, ...], publisher: str, trust_store: Any, now: Any
) -> Envelope:
    """The verified payload, or ``ReleaseRefused``; the signature is checked before any field is trusted."""
    from skeleton.system.packaging.errors import PackageError
    from skeleton.system.packaging.primitives import _json, _timestamp, canonical_json
    from skeleton.system.packaging.signing import _trusted_key, _verify

    try:
        parsed = _json(data)
    except PackageError:
        raise ReleaseRefused("invalid_envelope") from None
    if not isinstance(parsed, dict) or set(parsed) != {"payload", "signature"}:
        raise ReleaseRefused("invalid_envelope")
    payload, signature = parsed["payload"], parsed["signature"]
    if not isinstance(payload, dict) or set(payload) != PAYLOAD_FIELDS or not isinstance(signature, str):
        raise ReleaseRefused("invalid_envelope")
    try:
        key = _trusted_key(publisher, trust_store)
    except PackageError:
        raise ReleaseRefused("untrusted_publisher") from None
    try:
        _verify(key, signature, canonical_json(payload))
    except PackageError:
        raise ReleaseRefused("invalid_signature") from None
    try:
        signed_at, expires_at = _timestamp(payload["signed_at"]), _timestamp(payload["expires_at"])
    except PackageError:
        raise ReleaseRefused("invalid_envelope") from None
    if payload["client_id"] != client_id:
        raise ReleaseRefused("wrong_client")
    if payload["channel"] not in channels:
        raise ReleaseRefused("unknown_channel")
    if expires_at <= now or signed_at >= expires_at:
        raise ReleaseRefused("expired")
    if not isinstance(payload["feed_sha256"], str) or payload["feed_sha256"].lower() != sha256_hex(feed):
        raise ReleaseRefused("feed_hash_mismatch")
    return Envelope(client_id, payload["channel"], payload["feed_sha256"].lower(), signed_at, expires_at)


def parse_feed(data: bytes) -> Feed:
    """The files a Velopack feed lists and its newest version; anything unexpected is refused."""
    from packaging.version import Version

    from skeleton.system.packaging.errors import PackageError
    from skeleton.system.packaging.primitives import _json
    from skeleton.versioning import normalize_version

    try:
        parsed = _json(data)
    except PackageError:
        raise ReleaseRefused("invalid_feed") from None
    assets = parsed.get("Assets") if isinstance(parsed, dict) else None
    if not isinstance(assets, list) or not 0 < len(assets) <= MAX_ASSETS:
        raise ReleaseRefused("invalid_feed")
    files: dict[str, FeedFile] = {}
    versions = []
    for asset in assets:
        if not isinstance(asset, dict):
            raise ReleaseRefused("invalid_feed")
        name, digest, size = asset.get("FileName"), asset.get("SHA256"), asset.get("Size")
        if (
            not isinstance(name, str)
            or not FILE_NAME.fullmatch(name)
            or name.startswith("releases.")
            or not isinstance(digest, str)
            or not SHA256.fullmatch(digest)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            raise ReleaseRefused("invalid_feed")
        entry = FeedFile(name, digest.lower(), size)
        if files.get(name, entry) != entry:
            raise ReleaseRefused("invalid_feed")
        files[name] = entry
        version = asset.get("Version")
        try:
            versions.append((Version(normalize_version(version)), version))
        except (TypeError, ValueError):
            raise ReleaseRefused("invalid_feed") from None
    # The newest version as the publisher wrote it (SemVer), compared in its PEP 440 form.
    return Feed(version=max(versions)[1], files=tuple(files[name] for name in sorted(files)))
