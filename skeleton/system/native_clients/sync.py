"""Sync the signed releases of a native client from its source (#606).

For each release of the source (oldest first) and each envelope ``releases.<os>.sig.json``:

1. read the feed ``releases.<os>.json`` and the envelope (1 MiB each at most);
2. verify the envelope (signature by the configured publisher, client, channel, expiry, feed
   hash) and refuse a ``signed_at`` that is not newer than the feed already published for that
   channel and OS (anti-rollback; the same feed again is simply skipped);
3. download every file the feed lists that is not stored yet, hashing while writing, and refuse
   a SHA-256 or size that differs from the feed;
4. only then store the feed and the envelope and publish the release row.

Each published or refused feed leaves a ``NativeClientSync`` row (codes, never content), audited
by its table's triggers. A source failure leaves one ``failed`` row and publishes nothing.
"""

from __future__ import annotations

import hashlib
import logging
import re
import tempfile
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import services
from .config import load_config
from .github import GitHubSource, SourceError
from .verification import ReleaseRefused, parse_feed, sha256_hex, verify_envelope

ENVELOPE = re.compile(r"releases\.([a-z0-9][a-z0-9_-]{0,31})\.sig\.json\Z")
MAX_METADATA_BYTES = 1024 * 1024
logger = logging.getLogger("skeleton.system.native_clients")


def _max_file_bytes() -> int:
    return int(getattr(settings, "SKELETON_NATIVE_CLIENT_MAX_FILE_BYTES", 1024 * 1024 * 1024))


class _HashingSink:
    def __init__(self, handle: Any) -> None:
        self.handle = handle
        self.digest = hashlib.sha256()
        self.size = 0

    def write(self, chunk: bytes) -> None:
        self.digest.update(chunk)
        self.size += len(chunk)
        self.handle.write(chunk)


def _record(
    client_id: str, outcome: str, *, reason: str = "", tag: str = "", channel: str = "", os: str = "", version: str = ""
) -> Any:
    from .models import NativeClientSync

    return NativeClientSync.objects.create(
        client_id=client_id,
        outcome=outcome,
        reason_code=reason[:40],
        tag=tag[:200],
        channel=channel[:32],
        os=os[:32],
        version=version[:64],
    )


def _download_file(source: Any, asset: Any, client_id: str, expected: Any) -> None:
    with tempfile.TemporaryFile() as handle:
        sink = _HashingSink(handle)
        try:
            source.fetch(asset, sink, limit=min(_max_file_bytes(), max(expected.size, 0) or _max_file_bytes()))
        except SourceError as exc:
            raise ReleaseRefused(exc.code) from None
        if sink.size != expected.size or sink.digest.hexdigest() != expected.sha256:
            raise ReleaseRefused("file_hash_mismatch")
        handle.seek(0)
        services.store_blob(client_id, expected.sha256, handle)


def _publish(configuration: Any, source: Any, release: Any, os: str, catalogue: dict[str, Any], now: Any) -> Any:
    from io import BytesIO

    from .models import NativeClientRelease

    client_id = configuration.client_id
    feed_asset = release.assets.get(f"releases.{os}.json")
    if feed_asset is None:
        raise ReleaseRefused("feed_missing")
    try:
        feed = source.read(feed_asset, limit=MAX_METADATA_BYTES)
        envelope_bytes = source.read(release.assets[f"releases.{os}.sig.json"], limit=MAX_METADATA_BYTES)
    except SourceError as exc:
        raise ReleaseRefused(exc.code) from None
    envelope = verify_envelope(
        envelope_bytes,
        feed=feed,
        client_id=client_id,
        channels=configuration.channels,
        publisher=configuration.publisher,
        trust_store=load_config().trust_store,
        now=now,
    )
    published = NativeClientRelease.all_objects.filter(client_id=client_id, channel=envelope.channel, os=os)
    if published.filter(feed_sha256=envelope.feed_sha256).exists():
        return None  # Already published by an earlier sync.
    latest = published.order_by("-signed_at").first()
    if latest is not None and envelope.signed_at <= latest.signed_at:
        raise ReleaseRefused("stale_signature")
    parsed = parse_feed(feed)
    for entry in parsed.files:
        if services.blob_exists(client_id, entry.sha256):
            continue
        asset = release.assets.get(entry.name) or catalogue.get(entry.name)
        if asset is None:
            raise ReleaseRefused("file_missing")
        _download_file(source, asset, client_id, entry)
    services.store_blob(client_id, envelope.feed_sha256, BytesIO(feed))
    services.store_blob(client_id, sha256_hex(envelope_bytes), BytesIO(envelope_bytes))
    with transaction.atomic():
        NativeClientRelease.objects.create(
            client_id=client_id,
            channel=envelope.channel,
            os=os,
            version=parsed.version,
            tag=release.tag,
            feed_sha256=envelope.feed_sha256,
            envelope_sha256=sha256_hex(envelope_bytes),
            signed_at=envelope.signed_at,
            expires_at=envelope.expires_at,
            files=[{"name": item.name, "sha256": item.sha256, "size": item.size} for item in parsed.files],
        )
        return _record(client_id, "published", tag=release.tag, channel=envelope.channel, os=os, version=parsed.version)


def sync_client(client_id: str, *, source: Any = None, now: Any = None) -> list[Any]:
    """Publish the new verified feeds of one configured client; returns the history rows written."""
    from .models import NativeClientSync

    configuration = load_config().client(client_id)
    if configuration is None:
        raise SourceError("unknown_client")
    source = source or GitHubSource(configuration.repository, configuration.token_file)
    now = now or timezone.now()
    try:
        releases = source.releases()
    except SourceError as exc:
        return [_record(client_id, "failed", reason=exc.code)]
    catalogue: dict[str, Any] = {}
    for release in releases:
        for name, asset in release.assets.items():
            catalogue.setdefault(name, asset)
    written = []
    for release in releases:
        for name in sorted(release.assets):
            match = ENVELOPE.fullmatch(name)
            if match is None:
                continue
            os = match.group(1)
            try:
                with transaction.atomic():
                    row = _publish(configuration, source, release, os, catalogue, now)
            except ReleaseRefused as exc:
                logger.warning(
                    "native_client_release_refused",
                    extra={"event": "native_client_release_refused", "reason_code": exc.code},
                )
                already = NativeClientSync.objects.filter(
                    client_id=client_id, outcome="rejected", tag=release.tag[:200], os=os, reason_code=exc.code
                ).exists()
                if not already:  # The same refusal on every run would only repeat itself.
                    written.append(_record(client_id, "rejected", reason=exc.code, tag=release.tag, os=os))
                continue
            if row is not None:
                written.append(row)
    return written


def sync_all(**payload: Any) -> None:
    """Job ``native_clients.sync`` (runs in the job's system context): every enabled client."""
    for client_id, entry in sorted(load_config().clients.items()):
        if entry.enabled:
            sync_client(client_id)
