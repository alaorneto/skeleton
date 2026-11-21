"""Distribution of native client updates (#606): sync against a simulated GitHub, serving, policy, admin."""

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from io import StringIO
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.core.management import CommandError, call_command
from django.test import Client
from django.urls import reverse

from skeleton.system.native_clients.github import SourceAsset, SourceRelease
from skeleton.system.packaging.primitives import canonical_json
from skeleton.tenancy.context import request_context, system_context

CLIENT = "acme-desktop"
PASSWORD = "Fixture-only-password-2026!"  # noqa: S105 - disposable local fixture
NOW = datetime.now(UTC)


class FakeSource:
    """GitHub simulated in memory: releases (oldest first) and the bytes of each asset."""

    def __init__(self):
        self.blobs, self._releases, self._next = {}, [], 1

    def add(self, tag, files):
        assets = {}
        for name, data in files.items():
            assets[name] = SourceAsset(self._next, name, len(data))
            self.blobs[self._next] = data
            self._next += 1
        self._releases.append(SourceRelease(tag, assets))

    def releases(self):
        return list(self._releases)

    def fetch(self, asset, sink, *, limit):
        data = self.blobs[asset.id]
        sink.write(data)
        return len(data)

    def read(self, asset, *, limit):
        return self.blobs[asset.id]


def sign(key, payload):
    return base64.b64encode(key.sign(canonical_json(payload))).decode()


def release_files(key, version, *, channel="stable", signed_at=None, expires_at=None, payload_bytes=None, extra=()):
    package = f"AcmeDesktop-{version}-full.nupkg"
    content = f"package {version}".encode()
    assets = [
        {
            "PackageId": "AcmeDesktop",
            "Version": v,
            "Type": "Full",
            "FileName": f"AcmeDesktop-{v}-full.nupkg",
            "SHA256": hashlib.sha256(f"package {v}".encode()).hexdigest(),
            "Size": len(f"package {v}".encode()),
        }
        for v in (*extra, version)
    ]
    feed = json.dumps({"Assets": assets}, indent=2).encode()
    payload = {
        "client_id": CLIENT,
        "channel": channel,
        "feed_sha256": hashlib.sha256(feed).hexdigest(),
        "signed_at": (signed_at or NOW).isoformat(),
        "expires_at": (expires_at or NOW + timedelta(days=30)).isoformat(),
    }
    envelope = json.dumps({"payload": payload, "signature": sign(key, payload)}).encode()
    return {"releases.win.json": feed, "releases.win.sig.json": envelope, package: payload_bytes or content}


@pytest.fixture
def distribution(skeleton_db, settings, tmp_path):
    key = Ed25519PrivateKey.generate()
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    (tmp_path / "trust.json").write_text(json.dumps({"acme": public}), encoding="utf-8")
    config = {
        "trust_store": "trust.json",
        "clients": {CLIENT: {"repository": "acme/desktop", "channels": ["stable", "beta"], "publisher": "acme"}},
    }
    (tmp_path / "native.json").write_text(json.dumps(config), encoding="utf-8")
    settings.SKELETON_NATIVE_CLIENTS_CONFIG = str(tmp_path / "native.json")
    settings.STORAGES = {
        **settings.STORAGES,
        "skeleton_files": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": str(tmp_path / "files"), "base_url": None},
        },
    }
    return key


def sync(source):
    from skeleton.system.native_clients.sync import sync_client

    with system_context(reason="native-client:sync"):
        return sync_client(CLIENT, source=source)


def body(response):
    return b"".join(response.streaming_content) if response.streaming else response.content


def file_url(name, channel="stable"):
    return reverse("native_clients:file", kwargs={"client_id": CLIENT, "channel": channel, "name": name})


def test_sync_publishes_two_releases_and_serves_the_exact_bytes(distribution):
    source = FakeSource()
    first = release_files(distribution, "1.0.0", signed_at=NOW - timedelta(days=2))
    second = release_files(distribution, "1.1.0", signed_at=NOW - timedelta(days=1), extra=("1.0.0",))
    source.add("v1.0.0", first)
    source.add("v1.1.0", {key: value for key, value in second.items() if "1.0.0" not in key})
    rows = sync(source)
    assert [(row.outcome, row.version) for row in rows] == [("published", "1.0.0"), ("published", "1.1.0")]
    assert sync(source) == []  # Idempotent: nothing new.
    anonymous = Client()
    feed = anonymous.get(file_url("releases.win.json"))
    assert feed.status_code == 200 and body(feed) == second["releases.win.json"]
    assert feed["ETag"] == f'"{hashlib.sha256(second["releases.win.json"]).hexdigest()}"'
    assert "max-age" in feed["Cache-Control"]
    assert body(anonymous.get(file_url("releases.win.sig.json"))) == second["releases.win.sig.json"]
    package = anonymous.get(
        file_url("AcmeDesktop-1.0.0-full.nupkg")
    )  # From the first release, listed by the second feed.
    assert body(package) == b"package 1.0.0" and package["Content-Length"] == str(len(b"package 1.0.0"))
    again = anonymous.get(file_url("AcmeDesktop-1.0.0-full.nupkg"), HTTP_IF_NONE_MATCH=package["ETag"])
    assert again.status_code == 304
    for missing in ("other.nupkg", "releases.linux.json"):
        assert anonymous.get(file_url(missing)).status_code == 404
    assert anonymous.get(file_url("releases.win.json", channel="beta")).status_code == 404
    assert anonymous.post(file_url("releases.win.json")).status_code == 405
    policy = anonymous.get(
        reverse("api:native-client-policy", kwargs={"client_id": CLIENT}),
        {"version": "1.0.0", "os": "win", "channel": "stable"},
    ).json()
    assert policy == {"latest_version": "1.1.0", "minimum_version": None, "update_required": False}


def test_refusals_are_recorded_and_publish_nothing(distribution, subtests):
    from skeleton.audit.models import AuditEvent
    from skeleton.system.native_clients.models import NativeClientRelease, NativeClientSync

    other = Ed25519PrivateKey.generate()
    cases = {
        "invalid_signature": release_files(other, "1.0.0"),
        "expired": release_files(
            distribution, "1.0.1", expires_at=NOW - timedelta(seconds=1), signed_at=NOW - timedelta(days=1)
        ),
        "file_hash_mismatch": release_files(distribution, "1.0.2", payload_bytes=b"tampered package"),
    }
    for code, files in cases.items():
        with subtests.test(code):
            source = FakeSource()
            source.add(f"tag-{code}", files)
            (row,) = sync(source)
            assert (row.outcome, row.reason_code) == ("rejected", code)
    with subtests.test("stale_signature"):
        source = FakeSource()
        source.add("v2", release_files(distribution, "2.0.0", signed_at=NOW - timedelta(hours=1)))
        source.add("v1-late", release_files(distribution, "1.9.0", signed_at=NOW - timedelta(days=3)))
        rows = sync(source)
        assert [(row.outcome, row.reason_code) for row in rows] == [("published", ""), ("rejected", "stale_signature")]
    with system_context(reason="test:check"):
        assert NativeClientRelease.objects.filter(client_id=CLIENT).values_list("version", flat=True).get() == "2.0.0"
        rejected = NativeClientSync.objects.filter(outcome="rejected")
        assert rejected.count() == 4
        ids = list(rejected.values_list("pk", flat=True))
        from allauth.account.models import EmailAddress

        from skeleton.accounts.services import create_identity

        admin = create_identity(email="p-au@releases.example.test", password=PASSWORD)
        EmailAddress.objects.create(user=admin, email=admin.email, verified=True, primary=True)
        admin.is_staff = admin.is_superuser = True
        admin.save(update_fields=["is_staff", "is_superuser"])
    with request_context(actor=admin, request_id=uuid4(), source="test:audit"):
        assert AuditEvent.objects.filter(record_sysid__in=ids, operation="create").count() == 4


def test_config_refuses_a_publisher_outside_the_trust_store(distribution, settings, tmp_path):
    from django.core.exceptions import ImproperlyConfigured

    from skeleton.system.native_clients.config import load_config

    config = json.loads((tmp_path / "native.json").read_text("utf-8"))
    config["clients"][CLIENT]["publisher"] = "stranger"
    (tmp_path / "native.json").write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ImproperlyConfigured):
        load_config()
    with pytest.raises(CommandError):
        call_command("native_client_releases", "sync", stdout=StringIO())


def test_policy_minimum_withdraw_and_operator_only_administration(distribution):
    from skeleton.accounts.platform import grant_platform_role
    from skeleton.accounts.services import create_identity
    from skeleton.system.native_clients.models import NativeClientChannel, NativeClientRelease

    source = FakeSource()
    source.add("v1.0.0", release_files(distribution, "1.0.0", signed_at=NOW - timedelta(days=2)))
    source.add("v1.1.0", release_files(distribution, "1.1.0", signed_at=NOW - timedelta(days=1), extra=("1.0.0",)))
    sync(source)
    with system_context(reason="test:identities"):
        from allauth.account.models import EmailAddress

        people = {}
        for name in ("admin", "operator", "auditor"):
            people[name] = create_identity(email=f"p-{name[:2]}@releases.example.test", password=PASSWORD)
            EmailAddress.objects.create(user=people[name], email=people[name].email, verified=True, primary=True)
        people["admin"].is_staff = people["admin"].is_superuser = True
        people["admin"].save(update_fields=["is_staff", "is_superuser"])
    with request_context(actor=people["admin"], request_id=uuid4(), source="test:roles"):
        grant_platform_role(actor=people["admin"], user=people["operator"], role="operator")
        grant_platform_role(actor=people["admin"], user=people["auditor"], role="auditor")
    from tests import support

    operator = support.login(people["operator"], password=PASSWORD, csrf=False)
    auditor = support.login(people["auditor"], password=PASSWORD, csrf=False)
    page = reverse("platform:native-clients")
    assert operator.get(page).status_code == 200 and "acme-desktop" in operator.get(page).content.decode()
    assert auditor.get(page).status_code == 403
    minimum = reverse("platform:native-client-minimum", args=[CLIENT, "stable"])
    assert auditor.post(minimum, {"minimum_version": "9.0.0", "revision": 0}).status_code == 403
    assert Client().post(minimum, {"minimum_version": "9.0.0", "revision": 0}).status_code == 302  # Login.
    assert operator.post(minimum, {"minimum_version": "1.1.0", "revision": 0}).status_code == 302
    with system_context(reason="test:check"):
        assert NativeClientChannel.objects.get(client_id=CLIENT, channel="stable").minimum_version == "1.1.0"
    url = reverse("api:native-client-policy", kwargs={"client_id": CLIENT})
    policy = Client().get(url, {"version": "1.0.0", "os": "win", "channel": "stable"}).json()
    assert policy == {"latest_version": "1.1.0", "minimum_version": "1.1.0", "update_required": True}
    assert Client().get(url, {"version": "1.1.0", "os": "win", "channel": "stable"}).json()["update_required"] is False
    assert Client().get(url, {"version": "x", "os": "win", "channel": "stable"}).status_code == 400
    assert Client().get(url, {"version": "1.0.0", "os": "win", "channel": "nightly"}).status_code == 404
    with system_context(reason="test:check"):
        newest = NativeClientRelease.objects.get(version="1.1.0")
    withdraw = reverse("platform:native-client-withdraw", args=[newest.pk])
    assert auditor.post(withdraw, {"revision": newest.revision}).status_code == 403
    assert operator.post(withdraw, {"revision": newest.revision}).status_code == 302
    assert Client().get(url, {"version": "1.0.0", "os": "win", "channel": "stable"}).json()["latest_version"] == "1.0.0"
    assert Client().get(file_url("AcmeDesktop-1.1.0-full.nupkg")).status_code == 404


def test_github_source_follows_one_redirect_without_the_token(monkeypatch, tmp_path):
    from skeleton.common import outbound_http
    from skeleton.system.native_clients.github import GitHubSource, SourceError

    token = tmp_path / "token"
    token.write_text("ghp_fixture-token", encoding="utf-8")
    calls = []

    def fake_download(url, sink, *, headers, max_bytes, **kwargs):
        calls.append((url, headers.get("Authorization")))
        if url.startswith("https://api.github.com/"):
            return outbound_http.OutboundDownload(
                302, {"location": "https://objects.githubusercontent.com/x"}, 0, False
            )
        sink.write(b"bytes")
        return outbound_http.OutboundDownload(200, {}, 5, False)

    monkeypatch.setattr(outbound_http, "download", fake_download)
    source = GitHubSource("acme/desktop", token)
    assert source.read(SourceAsset(7, "a.nupkg", 5), limit=10) == b"bytes"
    assert calls == [
        ("https://api.github.com/repos/acme/desktop/releases/assets/7", "Bearer ghp_fixture-token"),
        ("https://objects.githubusercontent.com/x", None),
    ]

    def evil(url, sink, *, headers, max_bytes, **kwargs):
        return outbound_http.OutboundDownload(302, {"location": "https://evil.example.test/x"}, 0, False)

    monkeypatch.setattr(outbound_http, "download", evil)
    with pytest.raises(SourceError) as raised:
        source.read(SourceAsset(7, "a.nupkg", 5), limit=10)
    assert raised.value.code == "redirect_refused"
