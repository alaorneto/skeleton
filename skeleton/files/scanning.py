"""Optional antivirus for attachments (#62): ``none`` or ClamAV over ``clamd`` INSTREAM.

With a scanner configured (``SKELETON_FILES_SCANNER=clamav``) an upload is born ``pending``
and the ``files.scan`` job moves it to ``available`` (clean) or ``quarantined`` (signature
found). Downloads require ``available``, so a pending or quarantined file never leaves. A
scanner that is down raises in the job: the job retries and the attachment stays pending.
"""

from __future__ import annotations

import socket
import struct
from dataclasses import dataclass
from typing import IO, Any
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone
from django.utils.translation import gettext

SCAN_JOB = "files.scan"
CHUNK = 64 * 1024
TIMEOUT = 30.0


class ScannerUnavailable(Exception):
    """The scanner could not give a verdict; the job retries."""


@dataclass(frozen=True)
class Verdict:
    clean: bool
    signature: str = ""


def scanner_name() -> str:
    return str(getattr(settings, "SKELETON_FILES_SCANNER", "none") or "none")


def scanning_enabled() -> bool:
    return scanner_name() != "none"


class ClamAV:
    """``clamd`` INSTREAM over TCP (``tcp://host:3310``) or a Unix socket (``unix:///path``)."""

    def __init__(self, address: str, *, timeout: float = TIMEOUT):
        self.address = address
        self.timeout = timeout

    def _connect(self) -> socket.socket:
        parts = urlsplit(self.address)
        try:
            if parts.scheme == "tcp" and parts.hostname:
                return socket.create_connection((parts.hostname, parts.port or 3310), timeout=self.timeout)
            if parts.scheme == "unix" and parts.path and hasattr(socket, "AF_UNIX"):
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # type: ignore[attr-defined]
                client.settimeout(self.timeout)
                client.connect(parts.path)
                return client
        except OSError as exc:
            raise ScannerUnavailable(f"clamd unreachable: {type(exc).__name__}") from exc
        raise ImproperlyConfigured("SKELETON_FILES_CLAMAV_ADDRESS must be tcp://host:port or unix:///path.")

    def scan(self, stream: IO[bytes]) -> Verdict:
        try:
            with self._connect() as client:
                client.sendall(b"zINSTREAM\0")
                while chunk := stream.read(CHUNK):
                    client.sendall(struct.pack("!I", len(chunk)) + chunk)
                client.sendall(struct.pack("!I", 0))
                reply = b""
                while not reply.endswith(b"\0") and len(reply) < 4096:
                    data = client.recv(4096)
                    if not data:
                        break
                    reply += data
        except OSError as exc:
            raise ScannerUnavailable(f"clamd failed: {type(exc).__name__}") from exc
        return parse_reply(reply)


def parse_reply(reply: bytes) -> Verdict:
    """``stream: OK`` is clean; ``stream: <signature> FOUND`` is infected; anything else is no verdict."""
    text = reply.rstrip(b"\0").decode("utf-8", "replace").strip()
    _, _, status = text.partition(": ")
    if status == "OK":
        return Verdict(clean=True)
    if status.endswith(" FOUND"):
        return Verdict(clean=False, signature=status.removesuffix(" FOUND").strip()[:100])
    raise ScannerUnavailable(gettext("clamd gave no verdict"))


def get_scanner() -> Any:
    name = scanner_name()
    if name == "clamav":
        address = getattr(settings, "SKELETON_FILES_CLAMAV_ADDRESS", "")
        if not address:
            raise ImproperlyConfigured("SKELETON_FILES_SCANNER=clamav needs SKELETON_FILES_CLAMAV_ADDRESS.")
        return ClamAV(address)
    raise ImproperlyConfigured(f"Unknown SKELETON_FILES_SCANNER: {name}.")


def enqueue_scan(attachment: Any) -> None:
    from skeleton.jobs.services import enqueue

    enqueue(
        SCAN_JOB,
        organization_id=attachment.organization_id,
        payload={"attachment_id": str(attachment.pk)},
        idempotency_key=f"files-scan:{attachment.pk}",
    )


def scan_attachment(*, attachment_id: str) -> str:
    """Job handler (system context of the organization): decide a pending attachment."""
    from .models import Attachment
    from .storage import open_object

    attachment = Attachment.objects.with_deleted().select_for_update().filter(pk=attachment_id).first()
    if attachment is None or attachment.state != Attachment.State.PENDING:
        return "skipped"
    with open_object(attachment.storage_key) as stream:
        verdict = get_scanner().scan(stream)  # ScannerUnavailable: the job retries, still pending.
    if verdict.clean:
        attachment.state, attachment.scan_result_code = Attachment.State.AVAILABLE, "clean"
    else:
        attachment.state = Attachment.State.QUARANTINED
        attachment.scan_result_code = f"infected:{verdict.signature}"[:64]
    attachment.scanned_at = timezone.now()
    attachment.save(update_fields={"state", "scan_result_code", "scanned_at"})
    return attachment.state
