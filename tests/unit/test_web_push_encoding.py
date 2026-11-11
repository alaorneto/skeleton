"""Web push encoding (RFC 8291), VAPID (RFC 8292) and the endpoint allowlist (#56)."""

import json

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from skeleton.notifications.channels import push
from skeleton.notifications.channels.push import b64url, b64url_decode, encrypt, endpoint_allowed

# RFC 8291, appendix A.
PLAINTEXT = b"When I grow up, I want to be a watermelon"
AS_PRIVATE = "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"
UA_PRIVATE = "q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94"
UA_PUBLIC = "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
AUTH = "BTBZMqHH6r4Tts7J_aSIgg"
SALT = "DGv6ra1nlYgDCS1FRnbzlw"
EXPECTED = (
    "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_"
    "yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"
)


def private_key(value):
    return ec.derive_private_key(int.from_bytes(b64url_decode(value), "big"), ec.SECP256R1())


def decrypt(body, *, ua_private, auth):
    """What the browser does (RFC 8291 section 3): proves the body is readable by the subscriber."""
    salt, key_length = body[:16], body[20]
    sender_bytes = body[21 : 21 + key_length]
    receiver = private_key(ua_private)
    receiver_bytes = receiver.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    sender = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), sender_bytes)
    shared = receiver.exchange(ec.ECDH(), sender)
    ikm = HKDF(hashes.SHA256(), 32, b64url_decode(auth), b"WebPush: info\x00" + receiver_bytes + sender_bytes).derive(
        shared
    )
    key = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(ikm)
    padded = AESGCM(key).decrypt(nonce, body[21 + key_length :], None)
    assert padded.endswith(b"\x02")
    return padded[:-1]


def test_rfc8291_test_vector():
    body = encrypt(PLAINTEXT, p256dh=UA_PUBLIC, auth=AUTH, sender_key=private_key(AS_PRIVATE), salt=b64url_decode(SALT))
    assert b64url(body) == EXPECTED


def test_random_keys_round_trip_and_never_repeat():
    first = encrypt(b'{"title":"Aviso"}', p256dh=UA_PUBLIC, auth=AUTH)
    second = encrypt(b'{"title":"Aviso"}', p256dh=UA_PUBLIC, auth=AUTH)
    assert first != second
    assert decrypt(first, ua_private=UA_PRIVATE, auth=AUTH) == b'{"title":"Aviso"}'
    with pytest.raises(ValueError):
        encrypt(b"x" * 4096, p256dh=UA_PUBLIC, auth=AUTH)


@pytest.mark.parametrize(
    ("endpoint", "allowed"),
    [
        ("https://fcm.googleapis.com/fcm/send/abc", True),
        ("https://wns2-par02p.notify.windows.com/w/?token=x", True),
        ("https://notify.windows.com/w/", False),
        ("https://fcm.googleapis.com.evil.example/x", False),
        ("http://fcm.googleapis.com/fcm/send/abc", False),
        ("https://fcm.googleapis.com:8443/x", False),
        ("https://user:pass@fcm.googleapis.com/x", False),
        ("https://127.0.0.1/x", False),
        ("https://metadata.google.internal/x", False),
        ("not a url", False),
        (None, False),
    ],
)
def test_endpoint_allowlist(endpoint, allowed, settings):
    settings.WEBPUSH_ALLOWED_HOSTS = list(push.DEFAULT_ALLOWED_HOSTS)
    assert endpoint_allowed(endpoint) is allowed


def test_vapid_authorization_is_a_valid_es256_jwt(tmp_path, settings):
    key = ec.generate_private_key(ec.SECP256R1())
    path = tmp_path / "vapid.pem"
    path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    settings.WEBPUSH_VAPID_PRIVATE_KEY_FILE = str(path)
    settings.WEBPUSH_VAPID_SUBJECT = "mailto:ops@example.test"
    value = push.vapid_authorization("https://fcm.googleapis.com/fcm/send/abc", now=1_000_000)
    token, public = value.removeprefix("vapid t=").split(", k=")
    header, claims, signature = token.split(".")
    assert json.loads(b64url_decode(header)) == {"typ": "JWT", "alg": "ES256"}
    assert json.loads(b64url_decode(claims)) == {
        "aud": "https://fcm.googleapis.com",
        "exp": 1_000_000 + push.JWT_LIFETIME,
        "sub": "mailto:ops@example.test",
    }
    raw = b64url_decode(signature)
    verifier = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b64url_decode(public))
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    verifier.verify(der, f"{header}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
    assert public == push.public_key()
