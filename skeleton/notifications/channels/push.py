"""Web push with VAPID (#56): subscriptions, payload encryption and the push job.

The payload is encrypted with ``aes128gcm`` as in RFC 8291 and the request carries a VAPID
JWT (RFC 8292), both built with ``cryptography``; no push library is needed. Requests leave
only through ``skeleton.common.outbound_http`` and only to the push services in
``WEBPUSH_ALLOWED_HOSTS``. A ``404``/``410`` from the service removes the subscription.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.utils import timezone
from django.utils.translation import gettext, gettext_lazy

from .delivery import OPEN_STATES, active_recipient, failed_attempt, finish, open_url, target_readable

DEFAULT_ALLOWED_HOSTS = (
    "fcm.googleapis.com",
    "updates.push.services.mozilla.com",
    gettext_lazy("*.notify.windows.com"),
    "web.push.apple.com",
    gettext_lazy("*.push.apple.com"),
)
RECORD_SIZE = 4096
TTL_SECONDS = 24 * 3600
JWT_LIFETIME = 12 * 3600
GONE = (404, 410)


def b64url_decode(value: str) -> bytes:
    if not isinstance(value, str) or len(value) > 512:
        raise ValueError("invalid base64url")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


# --------------------------------------------------------------------------- endpoints


def allowed_hosts() -> tuple[str, ...]:
    return tuple(getattr(settings, "WEBPUSH_ALLOWED_HOSTS", DEFAULT_ALLOWED_HOSTS))


def endpoint_allowed(endpoint: Any) -> bool:
    """HTTPS on port 443 to an allowlisted push service; anything else is refused."""
    if not isinstance(endpoint, str) or not 1 <= len(endpoint) <= 2048:
        return False
    try:
        parts = urlsplit(endpoint)
        port = parts.port
    except ValueError:
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme != "https" or parts.username or parts.password or port not in (None, 443) or not host:
        return False
    for pattern in allowed_hosts():
        pattern = pattern.lower()
        if pattern.startswith("*.") and host.endswith(pattern[1:]) and host != pattern[2:]:
            return True
        if host == pattern:
            return True
    return False


def endpoint_digest(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode()).hexdigest()


def validate_subscription(endpoint: Any, p256dh: Any, auth: Any) -> None:
    from cryptography.hazmat.primitives.asymmetric import ec

    if not endpoint_allowed(endpoint):
        raise ValidationError({"endpoint": gettext("Serviço de push não permitido.")}, code="push_endpoint_refused")
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b64url_decode(p256dh))
        secret = b64url_decode(auth)
    except (ValueError, TypeError):
        raise ValidationError({"keys": gettext("Chaves da assinatura inválidas.")}) from None
    if len(secret) != 16:
        raise ValidationError({"keys": gettext("Chaves da assinatura inválidas.")})


# --------------------------------------------------------------------------- RFC 8291


def encrypt(payload: bytes, *, p256dh: str, auth: str, sender_key: Any = None, salt: bytes | None = None) -> bytes:
    """The ``aes128gcm`` body of RFC 8291 (one record); ``sender_key``/``salt`` exist for tests."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    receiver_bytes = b64url_decode(p256dh)
    receiver = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), receiver_bytes)
    secret = b64url_decode(auth)
    sender = sender_key or ec.generate_private_key(ec.SECP256R1())
    sender_bytes = sender.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    salt = salt or os.urandom(16)
    shared = sender.exchange(ec.ECDH(), receiver)
    ikm = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=secret, info=b"WebPush: info\x00" + receiver_bytes + sender_bytes
    ).derive(shared)
    key = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(ikm)
    if len(payload) > RECORD_SIZE - 17 - 86:
        raise ValueError("Push payload is too large.")
    ciphertext = AESGCM(key).encrypt(nonce, payload + b"\x02", None)
    header = salt + struct.pack("!IB", RECORD_SIZE, len(sender_bytes)) + sender_bytes
    return header + ciphertext


# --------------------------------------------------------------------------- RFC 8292


@dataclass(frozen=True)
class VapidKey:
    private: Any
    public: str  # base64url of the uncompressed P-256 point (applicationServerKey)


def push_configured() -> bool:
    path = getattr(settings, "WEBPUSH_VAPID_PRIVATE_KEY_FILE", "")
    return bool(path) and bool(getattr(settings, "WEBPUSH_VAPID_SUBJECT", "")) and os.path.isfile(path)


@lru_cache(maxsize=4)
def _load_key(path: str, mtime: float) -> VapidKey:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    with open(path, "rb") as handle:
        private = serialization.load_pem_private_key(handle.read(), password=None)
    if not isinstance(private, ec.EllipticCurvePrivateKey) or private.curve.name != "secp256r1":
        raise ImproperlyConfigured("WEBPUSH_VAPID_PRIVATE_KEY_FILE must hold a P-256 private key.")
    public = private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return VapidKey(private, b64url(public))


def vapid_key() -> VapidKey:
    path = getattr(settings, "WEBPUSH_VAPID_PRIVATE_KEY_FILE", "")
    if not push_configured():
        raise ImproperlyConfigured("Web push needs WEBPUSH_VAPID_PRIVATE_KEY_FILE and WEBPUSH_VAPID_SUBJECT.")
    return _load_key(path, os.path.getmtime(path))


def public_key() -> str:
    return vapid_key().public if push_configured() else ""


def vapid_authorization(endpoint: str, *, now: float | None = None) -> str:
    """``Authorization: vapid t=<JWT ES256>, k=<public key>`` for the push service origin."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    key = vapid_key()
    parts = urlsplit(endpoint)
    claims = {
        "aud": f"{parts.scheme}://{parts.netloc}",
        "exp": int((now or time.time()) + JWT_LIFETIME),
        "sub": settings.WEBPUSH_VAPID_SUBJECT,
    }
    header = b64url(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    body = b64url(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header}.{body}".encode()
    r, s = decode_dss_signature(key.private.sign(signing_input, ec.ECDSA(hashes.SHA256())))
    signature = b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={header}.{body}.{signature}, k={key.public}"


# --------------------------------------------------------------------------- subscriptions


def subscribe(user: Any, *, endpoint: Any, p256dh: Any, auth: Any, user_agent: str = "") -> Any:
    """Register (or refresh) the browser subscription of ``user``; the endpoint moves to them."""
    from ..models import WebPushSubscription

    validate_subscription(endpoint, p256dh, auth)
    digest = endpoint_digest(endpoint)
    current = WebPushSubscription.objects.select_for_update().filter(endpoint_digest=digest).first()
    if current is not None and current.user_id != user.pk:
        current.delete()  # Another person signed in on this browser: the old one stops receiving.
        current = None
    if current is not None:
        current.p256dh, current.auth_secret = p256dh, auth
        current.user_agent = user_agent[:200]
        current.save(update_fields={"p256dh", "auth_secret", "user_agent"})
        return current
    subscription = WebPushSubscription(
        user_id=user.pk,
        endpoint=endpoint,
        endpoint_digest=digest,
        p256dh=p256dh,
        auth_secret=auth,
        user_agent=user_agent[:200],
    )
    subscription.save()
    return subscription


def unsubscribe(user: Any, *, endpoint: Any) -> bool:
    from ..models import WebPushSubscription

    if not isinstance(endpoint, str):
        return False
    current = (
        WebPushSubscription.objects.select_for_update()
        .filter(endpoint_digest=endpoint_digest(endpoint), user_id=user.pk)
        .first()
    )
    if current is None:
        return False
    current.delete()
    return True


# --------------------------------------------------------------------------- job


def payload_for(notification: Any, user_id: Any) -> bytes:
    """Minimal payload: the type label and a link; the snapshot only when the type allows it."""
    from ..registry import get_notification_type

    try:
        kind = get_notification_type(notification.type_code)
    except KeyError:
        kind = None
    data: dict[str, Any] = {
        "title": str(kind.label) if kind else gettext("Notificação"),
        "url": open_url(notification, user_id),
    }
    if kind is not None and kind.push_snapshot and notification.snapshot:
        data["body"] = " · ".join(str(value) for value in notification.snapshot.values())[:240]
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode()


def _still_wanted(user: Any, type_code: str) -> bool:
    from ..preferences import effective, known_type

    return known_type(type_code) is not None and effective(user, type_code, "push").mode == "immediate"


def send_push(*, delivery_id: str) -> None:
    """Job handler (system context of the organization); never raises for a push outcome."""
    from skeleton.common.outbound_http import OutboundRequestFailed, OutboundRequestRefused, request

    from ..models import NotificationDelivery, WebPushSubscription

    delivery = NotificationDelivery.objects.select_for_update().filter(pk=delivery_id).first()
    if delivery is None or delivery.state not in OPEN_STATES:
        return
    user = active_recipient(delivery)
    if user is None:
        finish(delivery, "skipped", error_code="recipient_inactive")
        return
    notification = delivery.notification
    if notification is None or not _still_wanted(user, notification.type_code):
        finish(delivery, "skipped", error_code="opted_out")
        return
    if not target_readable(notification, user):
        finish(delivery, "skipped", error_code="target_unreadable")
        return
    if not push_configured():
        finish(delivery, "skipped", error_code="push_unconfigured")
        return
    subscriptions = list(WebPushSubscription.objects.select_for_update().filter(user_id=user.pk).order_by("sysid"))
    if not subscriptions:
        finish(delivery, "skipped", error_code="no_subscription")
        return
    payload = payload_for(notification, user.pk)
    sent, retry, error = 0, False, ""
    for subscription in subscriptions:
        endpoint = subscription.endpoint.reveal()
        if not endpoint_allowed(endpoint):
            subscription.delete()
            continue
        body = encrypt(payload, p256dh=subscription.p256dh.reveal(), auth=subscription.auth_secret.reveal())
        headers = {
            "Authorization": vapid_authorization(endpoint),
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
            "TTL": str(TTL_SECONDS),
            "Urgency": "normal",
        }
        try:
            response = request("POST", endpoint, headers=headers, body=body)
        except OutboundRequestRefused as exc:
            error = exc.code
            continue
        except OutboundRequestFailed:
            retry, error = True, "network_error"
            continue
        if 200 <= response.status < 300:
            sent += 1
            subscription.last_success_at = timezone.now()
            subscription.save(update_fields={"last_success_at"})
        elif response.status in GONE:
            subscription.delete()  # The browser revoked it: logical delete, never retried.
        else:
            error = f"http_{response.status}"
            retry = retry or response.status == 429 or response.status >= 500
    if sent:
        finish(delivery, "sent", attempts=delivery.attempts + 1, sent_at=timezone.now(), error_code="")
    elif retry:
        failed_attempt(delivery, error)
    else:
        finish(delivery, "skipped" if not error else "dead", attempts=delivery.attempts + 1, error_code=error or "gone")
