"""Envelope and feed verification of native client releases (#606), without the database."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from skeleton.system.native_clients.verification import ReleaseRefused, parse_feed, verify_envelope

VECTOR = json.loads((Path(__file__).parents[1] / "fixtures" / "native_clients" / "vector.json").read_text("utf-8"))
NOW = datetime(2026, 11, 21, tzinfo=UTC)


def verify(envelope=None, *, feed=None, publisher="acme", trust=None, channels=("stable",), now=NOW):
    return verify_envelope(
        json.dumps(envelope or VECTOR["envelope"]).encode(),
        feed=(feed or VECTOR["feed"]).encode() if isinstance(feed or VECTOR["feed"], str) else feed,
        client_id="acme-desktop",
        channels=channels,
        publisher=publisher,
        trust_store=trust or {"acme": VECTOR["public_key"]},
        now=now,
    )


def test_fixed_vector_verifies_and_its_canonical_payload_is_stable():
    from skeleton.system.packaging.primitives import canonical_json

    envelope = verify()
    assert envelope.channel == "stable" and envelope.feed_sha256 == VECTOR["envelope"]["payload"]["feed_sha256"]
    assert canonical_json(VECTOR["envelope"]["payload"]).decode() == VECTOR["canonical_payload"]


def test_refusals_have_stable_codes(subtests):
    payload = dict(VECTOR["envelope"]["payload"])
    cases = {
        "invalid_signature": {"payload": {**payload, "channel": "beta"}, "signature": VECTOR["envelope"]["signature"]},
        "invalid_envelope": {"payload": payload},
    }
    for code, envelope in cases.items():
        with subtests.test(code):
            with pytest.raises(ReleaseRefused) as raised:
                verify(envelope, channels=("stable", "beta"))
            assert raised.value.code == code
    with subtests.test("untrusted_publisher"), pytest.raises(ReleaseRefused) as raised:
        verify(trust={"other": VECTOR["public_key"]})
    assert raised.value.code == "untrusted_publisher"
    with subtests.test("expired"), pytest.raises(ReleaseRefused) as raised:
        verify(now=datetime(2127, 1, 1, tzinfo=UTC))
    assert raised.value.code == "expired"
    with subtests.test("feed_hash_mismatch"), pytest.raises(ReleaseRefused) as raised:
        verify(feed=VECTOR["feed"] + " ")
    assert raised.value.code == "feed_hash_mismatch"
    with subtests.test("unknown_channel"), pytest.raises(ReleaseRefused) as raised:
        verify(channels=("beta",))
    assert raised.value.code == "unknown_channel"


def test_feed_lists_files_and_the_newest_semver(subtests):
    feed = parse_feed(VECTOR["feed"].encode())
    assert feed.version == "1.2.3" and [item.name for item in feed.files] == ["AcmeDesktop-1.2.3-full.nupkg"]
    asset = json.loads(VECTOR["feed"])["Assets"][0]
    two = {"Assets": [asset, {**asset, "Version": "1.10.0-beta.1", "FileName": "AcmeDesktop-1.10.0-beta.1-full.nupkg"}]}
    assert parse_feed(json.dumps(two).encode()).version == "1.10.0-beta.1"  # 1.10 > 1.2, compared as versions.
    bad = {
        "path": {**asset, "FileName": "../etc/passwd"},
        "feed name": {**asset, "FileName": "releases.win.json"},
        "hash": {**asset, "SHA256": "zz"},
        "size": {**asset, "Size": -1},
        "version": {**asset, "Version": "latest"},
    }
    for name, entry in bad.items():
        with subtests.test(name), pytest.raises(ReleaseRefused):
            parse_feed(json.dumps({"Assets": [entry]}).encode())
