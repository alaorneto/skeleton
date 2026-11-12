"""Private object storage for attachments: local filesystem or S3, never public.

Keys are ``org/<organization>/<attachment>`` (``branding/`` for the logo, ``transfers/``
for CSV exports): two UUIDs and nothing chosen by
the user, so a file name can never traverse paths, collide or leak in a URL.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import IO, Any
from uuid import UUID

from django.core.files.base import File
from django.core.files.storage import storages

ALIAS = "skeleton_files"
KEY = re.compile(
    r"^org/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/(?:branding/|transfers/)?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
CHUNK = 64 * 1024


class StorageError(Exception):
    pass


def private_storage():
    return storages[ALIAS]


def object_key(organization_id: UUID | str, attachment_id: UUID | str) -> str:
    return f"org/{UUID(str(organization_id))}/{UUID(str(attachment_id))}"


def _checked(key: str) -> str:
    if not isinstance(key, str) or not KEY.fullmatch(key):
        raise StorageError("Invalid object key.")
    return key


def put_stream(key: str, stream: IO[bytes], *, max_bytes: int) -> int:
    """Store the stream under ``key`` exactly (never renamed); refuse more than ``max_bytes``."""
    storage = private_storage()
    key = _checked(key)
    if storage.exists(key):
        raise StorageError("Object already exists.")
    size = 0

    class Limited(File):
        def chunks(self, chunk_size: int | None = None) -> Iterator[bytes]:
            nonlocal size
            while True:
                chunk = stream.read(chunk_size or CHUNK)
                if not chunk:
                    return
                size += len(chunk)
                if size > max_bytes:
                    raise StorageError("Object too large.")
                yield chunk

    try:
        saved = storage.save(key, Limited(stream, name=key))
    except StorageError:
        delete(key)
        raise
    if saved != key:
        storage.delete(saved)
        raise StorageError("Storage renamed the object.")
    return size


def open_object(key: str) -> IO[bytes]:
    return private_storage().open(_checked(key), "rb")


def delete(key: str) -> None:
    storage = private_storage()
    key = _checked(key)
    if storage.exists(key):
        storage.delete(key)


def presigned_url(key: str, *, filename: str, expires: int = 60) -> str | None:
    """A short-lived S3 URL with a forced download disposition, or None for local storage."""
    storage = private_storage()
    key = _checked(key)
    if not hasattr(storage, "bucket"):
        return None
    if expires > 60:
        raise StorageError("Presigned URLs live at most 60 seconds.")
    from urllib.parse import quote

    parameters = {
        "ResponseContentDisposition": f"attachment; filename*=UTF-8''{quote(filename)}",
        "ResponseContentType": "application/octet-stream",
    }
    return storage.url(key, parameters=parameters, expire=expires)


def list_prefix(prefix: str) -> list[str]:
    """Keys under ``org/<organization>/`` or its ``branding/`` (used by orphan cleanup)."""
    if not re.fullmatch(r"org/[0-9a-f-]{36}(?:/branding)?/?", prefix):
        raise StorageError("Invalid prefix.")
    storage = private_storage()
    prefix = prefix.rstrip("/")
    try:
        _, files = storage.listdir(prefix)
    except FileNotFoundError:
        return []
    return sorted(f"{prefix}/{name}" for name in files)


def storage_problems(config: dict[str, Any] | None, *, static_root: Any) -> list[str]:
    """Why a STORAGES["skeleton_files"] entry is not private enough for production."""
    from pathlib import Path

    if not config:
        return ["STORAGES['skeleton_files'] is missing."]
    backend = config.get("BACKEND", "")
    options = config.get("OPTIONS", {}) or {}
    problems = []
    if backend.endswith("FileSystemStorage"):
        location = options.get("location")
        if not location:
            problems.append("Local private storage needs an explicit location.")
        else:
            root = Path(location).resolve()
            if static_root and (root == Path(static_root).resolve() or Path(static_root).resolve() in root.parents):
                problems.append("Private storage must not live inside STATIC_ROOT.")
        if options.get("base_url"):
            problems.append("Private storage must not have a public base_url.")
    elif "s3" in backend.lower():
        if options.get("default_acl") not in (None, "private"):
            problems.append("S3 private storage must not use a public ACL.")
        if options.get("querystring_auth") is False:
            problems.append("S3 private storage must sign URLs (querystring_auth).")
        if options.get("custom_domain"):
            problems.append("S3 private storage must not use a public custom domain.")
    else:
        problems.append(f"Unsupported private storage backend: {backend}.")
    return problems
