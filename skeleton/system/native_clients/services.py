"""What is served and what the release operator changes (#606).

Reading (anonymous surfaces): the live feed of a channel and OS is the newest release that is
not withdrawn and whose envelope has not expired; a file is served only while a live feed of the
channel lists it. Writing (platform): only the platform administrator or the release operator
(``updates.operate``), acting as the request actor, with the revision the form displayed;
every change is a row change of an audited table.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from skeleton.common.exceptions import ConcurrentUpdate

from .config import load_config

STORAGE = "skeleton_files"
PREFIX = "native-clients"


def _storage() -> Any:
    from django.core.files.storage import storages

    return storages[STORAGE]


def blob_key(client_id: str, sha256: str) -> str:
    """Content addressed: a stored blob is never rewritten, and its name is its verified hash."""
    return f"{PREFIX}/{client_id}/{sha256[:2]}/{sha256}"


def blob_exists(client_id: str, sha256: str) -> bool:
    return _storage().exists(blob_key(client_id, sha256))


def store_blob(client_id: str, sha256: str, handle: Any) -> None:
    from django.core.files import File

    storage = _storage()
    key = blob_key(client_id, sha256)
    if storage.exists(key):
        return
    saved = storage.save(key, File(handle))
    if saved != key:  # A concurrent sync stored the same verified bytes first.
        storage.delete(saved)


def open_blob(client_id: str, sha256: str) -> Any:
    return _storage().open(blob_key(client_id, sha256), "rb")


# --------------------------------------------------------------------------- reading


def _enabled(client_id: str) -> bool:
    return load_config().client(client_id) is not None


def live_release(client_id: str, channel: str, os: str) -> Any | None:
    from .models import NativeClientRelease

    if not _enabled(client_id):
        return None
    return (
        NativeClientRelease.objects.filter(
            client_id=client_id, channel=channel, os=os, withdrawn_at__isnull=True, expires_at__gt=timezone.now()
        )
        .order_by("-signed_at")
        .first()
    )


def served_file(client_id: str, channel: str, name: str) -> dict[str, Any] | None:
    """The ``{name, sha256, size}`` of a file listed by a live feed of the channel, else ``None``."""
    from .models import NativeClientRelease

    if not _enabled(client_id):
        return None
    systems = (
        NativeClientRelease.objects.filter(client_id=client_id, channel=channel).values_list("os", flat=True).distinct()
    )
    for os in sorted(set(systems)):
        release = live_release(client_id, channel, os)
        for entry in release.files if release is not None else ():
            if entry.get("name") == name:
                return entry
    return None


def policy(*, client_id: str, channel: str, os: str, version: str) -> dict[str, Any] | None:
    """``latest_version``, ``minimum_version`` and ``update_required``; ``None`` for an unknown channel."""
    from packaging.version import Version

    from skeleton.versioning import normalize_version

    from .models import NativeClientChannel

    configuration = load_config().client(client_id)
    if configuration is None or channel not in configuration.channels:
        return None
    current = Version(normalize_version(version))  # ValueError for an invalid version: the caller answers 400.
    release = live_release(client_id, channel, os)
    row = NativeClientChannel.objects.filter(client_id=client_id, channel=channel).first()
    minimum = row.minimum_version if row is not None and row.minimum_version else None
    return {
        "latest_version": release.version if release is not None else None,
        "minimum_version": minimum,
        "update_required": bool(minimum and current < Version(normalize_version(minimum))),
    }


def overview() -> list[dict[str, Any]]:
    """Clients of the configuration with their channels, releases and the recent sync history."""
    from .models import NativeClientChannel, NativeClientRelease, NativeClientSync

    configuration = load_config()
    result = []
    for client_id in sorted(configuration.clients):
        entry = configuration.clients[client_id]
        policies = {row.channel: row for row in NativeClientChannel.objects.filter(client_id=client_id)}
        result.append(
            {
                "client_id": client_id,
                "repository": entry.repository,
                "enabled": entry.enabled,
                "channels": [
                    {
                        "name": channel,
                        "minimum_version": policies[channel].minimum_version if channel in policies else "",
                        "revision": policies[channel].revision if channel in policies else 0,
                    }
                    for channel in entry.channels
                ],
                "releases": list(
                    NativeClientRelease.objects.filter(client_id=client_id).order_by("-signed_at", "os")[:50]
                ),
                "history": list(NativeClientSync.objects.filter(client_id=client_id).order_by("-created_at")[:50]),
            }
        )
    return result


# --------------------------------------------------------------------------- writing (platform)


def _require_operator(actor: Any) -> None:
    from skeleton.accounts.platform import UPDATES_OPERATE, require_platform_context

    require_platform_context(actor, capability=UPDATES_OPERATE, message="Release operator required.")


def set_minimum_version(
    *, actor: Any, client_id: str, channel: str, minimum_version: str, expected_revision: int
) -> Any:
    """Set (or clear, with ``""``) the minimum version of a channel; revision ``0`` creates it."""
    from skeleton.versioning import normalize_version

    from .models import NativeClientChannel

    _require_operator(actor)
    configuration = load_config().client(client_id)
    if configuration is None or channel not in configuration.channels:
        raise ValidationError({"channel": "Canal desconhecido."})
    minimum_version = (minimum_version or "").strip()
    if minimum_version:
        try:
            normalize_version(minimum_version)
        except ValueError:
            raise ValidationError({"minimum_version": "Use uma versão X.Y.Z."}) from None
    with transaction.atomic():
        row = NativeClientChannel.objects.select_for_update().filter(client_id=client_id, channel=channel).first()
        if (row.revision if row is not None else 0) != expected_revision:
            raise ConcurrentUpdate("The channel changed since the form was displayed.")
        if row is None:
            return NativeClientChannel.objects.create(
                client_id=client_id, channel=channel, minimum_version=minimum_version
            )
        if row.minimum_version != minimum_version:
            row.minimum_version = minimum_version
            row.save(update_fields=["minimum_version"])
        return row


def withdraw_release(*, actor: Any, release_id: Any, expected_revision: int) -> Any:
    """Stop serving a feed: the channel falls back to the previous live feed of the same OS."""
    from .models import NativeClientRelease

    _require_operator(actor)
    with transaction.atomic():
        release = NativeClientRelease.objects.select_for_update().filter(pk=release_id).first()
        if release is None:
            raise ValidationError({"release": "Versão desconhecida."})
        if release.revision != expected_revision:
            raise ConcurrentUpdate("The release changed since the page was displayed.")
        if release.withdrawn_at is None:
            release.withdrawn_at = timezone.now()
            release.save(update_fields=["withdrawn_at"])
        return release
