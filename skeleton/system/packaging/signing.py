"""Ed25519 keys, signature checks and the operator trust store."""

import os
from base64 import b64decode
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from . import limits
from .errors import PackageError, _fail
from .primitives import _json, _object


def _public_key(value):
    if isinstance(value, Ed25519PublicKey):
        return value
    if isinstance(value, dict):
        _object(value, ("public_key", "publisher"), ("public_key",))
        value = value["public_key"]
    if isinstance(value, Ed25519PublicKey):
        return value
    try:
        data = value.encode() if isinstance(value, str) and "-----BEGIN" in value else Path(value).read_bytes()
        key = serialization.load_pem_public_key(data)
    except (ValueError, TypeError, OSError) as exc:
        raise PackageError("Invalid trusted publisher key.") from exc
    if not isinstance(key, Ed25519PublicKey):
        _fail("Publisher keys must be Ed25519.")
    return key


def _trusted_key(publisher, trust_store):
    if not isinstance(trust_store, dict) or publisher not in trust_store:
        _fail("Publisher is not trusted.")
    value = trust_store[publisher]
    if isinstance(value, dict) and value.get("publisher", publisher) != publisher:
        _fail("Publisher trust restriction mismatch.")
    return _public_key(value)


def _private_key(path):
    try:
        key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as exc:
        raise PackageError("Invalid private signing key.") from exc
    if not isinstance(key, Ed25519PrivateKey):
        _fail("Signing key must be Ed25519.")
    return key


def _verify(key, signature, data):
    try:
        key.verify(b64decode(signature, validate=True), data)
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise PackageError("Invalid release signature.") from exc


def initialize_keys(private_path, public_path):
    private, public = Path(private_path), Path(public_path)
    if private == public or private.exists() or public.exists():
        _fail("Key output paths must be new and distinct.")
    key = Ed25519PrivateKey.generate()
    private.parent.mkdir(parents=True, exist_ok=True)
    public.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(private, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
        )
    with public.open("xb") as handle:
        handle.write(
            key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        )
    return private, public


def load_trust_store(path):
    """Operator trust store: publisher -> PEM text, PEM path or {'public_key', 'publisher'}.

    Relative PEM paths resolve against the trust store's own directory, never the artifact or the
    CWD. One implementation for the package command and the core distribution commands (#255).
    """
    path = Path(path)
    if path.stat().st_size > limits.MAX_METADATA_BYTES:
        _fail("Trust store is too large.")
    store = _json(path.read_bytes())
    if not isinstance(store, dict):
        _fail("Trust store must map publishers to operator public keys.")
    # Relative PEM paths are relative to operator config, never artifact.
    result = {}
    for publisher, value in store.items():
        if isinstance(value, str) and "-----BEGIN" not in value:
            value = str(path.parent / value) if not Path(value).is_absolute() else value
        elif isinstance(value, dict):
            value = dict(value)
            public = value.get("public_key")
            if isinstance(public, str) and "-----BEGIN" not in public:
                value["public_key"] = str(path.parent / public) if not Path(public).is_absolute() else public
        result[publisher] = value
    return result
