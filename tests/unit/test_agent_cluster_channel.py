"""The channel between agents (#680): signed requests over mutual TLS, artifacts and forwarding.

The listener runs in a thread on 127.0.0.1 with a CA and certificates generated here; the service
and the coordinator are fakes, so the test needs neither PostgreSQL nor the agent's journal.
"""

import datetime
import hashlib
import ipaddress
import json
import os
import ssl
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from deployment_agent import cluster_channel
from deployment_agent.cluster_channel import ClusterChannel, sign, verify
from deployment_agent.storage import AgentError

SECRET = b"s" * 40
# The control routes (Unix socket server) and the journal's atomic writes are POSIX only.
POSIX = pytest.mark.skipif(os.name != "posix", reason="Linux ou macOS: socket Unix e O_DIRECTORY.")


def _name(text):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, text)])


def _certificate(subject, issuer, key, issuer_key, *, ca=False, san=None):
    now = datetime.datetime.now(datetime.UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(subject))
        .issuer_name(_name(issuer))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    if san:
        builder = builder.add_extension(x509.SubjectAlternativeName(san), critical=False)
    return builder.sign(issuer_key, hashes.SHA256())


def _write(path, certificate, key):
    path.with_suffix(".crt").write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    path.with_suffix(".key").write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    root = tmp_path_factory.mktemp("pki")
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = _certificate("cluster-ca", "cluster-ca", ca_key, ca_key, ca=True)
    _write(root / "ca", ca, ca_key)
    san = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    for node in ("node-a", "node-b"):
        key = ec.generate_private_key(ec.SECP256R1())
        _write(root / node, _certificate(node, "cluster-ca", key, ca_key, san=san), key)
    other_key = ec.generate_private_key(ec.SECP256R1())
    other = _certificate("other-ca", "other-ca", other_key, other_key, ca=True)
    _write(root / "other-ca", other, other_key)
    stranger_key = ec.generate_private_key(ec.SECP256R1())
    _write(root / "stranger", _certificate("stranger", "other-ca", stranger_key, other_key, san=san), stranger_key)
    return root


class Coordinator:
    def __init__(self, *, primary, url=""):
        self.is_primary = primary
        self.epoch = 3 if primary else None
        self.url = url
        self.target = None

    def agent_target(self):
        return self.target

    def nodes(self):
        return [{"node_id": "node-a"}, {"node_id": "node-b"}]

    def primary(self):
        return {"node_id": "node-a", "epoch": 3, "cluster_url": self.url}


class Store:
    def __init__(self, root):
        self.root = root

    def path(self, collection, key):
        target = self.root / collection
        target.mkdir(parents=True, exist_ok=True)
        return target / key

    def plans(self):
        return self.plan_rows


class Service:
    def __init__(self, root, *, node, pki, primary, url=""):
        secret = root / f"{node}.secret"
        secret.write_bytes(SECRET)
        self.config = {
            "max_upload_bytes": 1024 * 1024,
            "cluster": {
                "node_id": node,
                "listen": "127.0.0.1:0",
                "cluster_url": url,
                "tls_cert": str(pki / f"{node}.crt"),
                "tls_key": str(pki / f"{node}.key"),
                "tls_ca": str(pki / "ca.crt"),
                "secret_file": str(secret),
            },
        }
        self.coordinator = Coordinator(primary=primary, url=url)
        self.store = Store(root / node)
        self.store.plan_rows = []
        self.calls = []

    def state(self):
        return {"inventory": {"core": {}, "apps": []}}

    def sources(self):
        self.calls.append("sources")
        return {"sources": ["github"]}

    def preview(self, action, artifacts, app_ids):
        self.calls.append(("preview", action))
        return {"id": "plan-1", "state": "preparing"}

    def upload(self, data):
        return {"digest": hashlib.sha256(data).hexdigest()}


@pytest.fixture
def pair(tmp_path, pki, monkeypatch):
    monkeypatch.setattr(cluster_channel, "read_secret", lambda path: Path(path).read_bytes())
    primary_service = Service(tmp_path, node="node-a", pki=pki, primary=True)
    primary = ClusterChannel(primary_service)
    primary.start()
    port = primary.server.server_address[1]
    url = f"https://localhost:{port}"
    primary_service.coordinator.url = url
    follower_service = Service(tmp_path, node="node-b", pki=pki, primary=False, url=url)
    follower = ClusterChannel(follower_service)
    yield primary_service, primary, follower_service, follower
    primary.stop()


def test_signatures_bind_method_path_time_node_epoch_and_body():
    header = sign(SECRET, method="POST", path="/v1/forward", node_id="node-b", epoch=0, body=b"{}", now=1000)
    assert verify(SECRET, header, method="POST", path="/v1/forward", body=b"{}", now=1030) == ("node-b", 0)
    for change in (
        {"method": "GET"},
        {"path": "/v1/uploads"},
        {"body": b"{ }"},
        {"now": 1000 + cluster_channel.MAX_SKEW_S + 1},
    ):
        arguments = {"method": "POST", "path": "/v1/forward", "body": b"{}", "now": 1000, **change}
        with pytest.raises(AgentError) as refused:
            verify(SECRET, header, **arguments)
        assert refused.value.status == 401
    with pytest.raises(AgentError):
        verify(b"x" * 40, header, method="POST", path="/v1/forward", body=b"{}", now=1000)


@POSIX
def test_a_follower_forwards_requests_and_keeps_its_own_state_local(pair):
    primary_service, _, follower_service, follower = pair
    assert follower.should_forward("POST", "/preview") and follower.should_forward("GET", "/operations/x")
    assert not follower.should_forward("GET", "/cluster") and not follower.should_forward("GET", "/inventory")
    assert follower.forward("GET", "/sources", None) == {"sources": ["github"]}
    assert follower.forward("POST", "/preview", {"action": "install", "artifacts": []})["id"] == "plan-1"
    assert primary_service.calls == ["sources", ("preview", "install")]
    with pytest.raises(AgentError) as unknown:
        follower.forward("POST", "/nowhere", {})
    assert unknown.value.code == "not_found"
    assert (
        follower.forward("POST", "/uploads", b"package-bytes")["digest"] == hashlib.sha256(b"package-bytes").hexdigest()
    )


@POSIX
def test_artifacts_come_by_digest_only_when_referenced_and_are_checked(pair):
    primary_service, _, follower_service, follower = pair
    data = b"signed package"
    digest = hashlib.sha256(data).hexdigest()
    primary_service.store.path("uploads", digest).write_bytes(data)
    with pytest.raises(AgentError) as hidden:
        follower.fetch_artifact(digest)  # Not in a plan nor in the active state.
    assert hidden.value.code == "not_found"
    primary_service.store.plan_rows = [("plan-1", {"plan": {"artifacts": [digest]}}, 0.0)]
    path = follower.fetch_artifact(digest)
    assert path.read_bytes() == data and path.parent == follower_service.store.root / "uploads"
    # The package of the agent version the cluster follows (#689) is served too.
    agent_data = b"agent release package"
    agent_digest = hashlib.sha256(agent_data).hexdigest()
    primary_service.store.path("uploads", agent_digest).write_bytes(agent_data)
    primary_service.coordinator.target = {"version": "0.7.0", "release": agent_digest, "epoch": 3}
    assert follower.fetch_artifact(agent_digest).read_bytes() == agent_data
    # A byte changed on the way (or on the primary): refused, nothing kept.
    tampered = b"tampered package"
    forged = hashlib.sha256(b"what the plan approved").hexdigest()
    primary_service.store.path("uploads", forged).write_bytes(tampered)
    primary_service.store.plan_rows = [("plan-2", {"plan": {"artifacts": [forged]}}, 0.0)]
    with pytest.raises(AgentError, match="package_verification_failed"):
        follower.fetch_artifact(forged)
    assert not follower_service.store.path("uploads", forged).exists()


def test_without_the_ca_certificate_or_the_secret_the_listener_refuses(pair, pki):
    _, primary, _, follower = pair
    url = follower._primary_url()
    status, data = follower.request("GET", "/v1/health", url=url)
    assert status == 200 and json.loads(data) == {"is_primary": True, "node_id": "node-a"}
    follower.secret = b"y" * 40
    status, _ = follower.request("GET", "/v1/health", url=url)
    assert status == 401
    follower.secret = SECRET
    follower.cluster = {**follower.cluster, "tls_cert": str(pki / "stranger.crt"), "tls_key": str(pki / "stranger.key")}
    with pytest.raises((AgentError, ssl.SSLError)):
        follower.request("GET", "/v1/health", url=url)


def test_a_follower_without_a_primary_answers_no_primary(tmp_path, pki, monkeypatch):
    monkeypatch.setattr(cluster_channel, "read_secret", lambda path: Path(path).read_bytes())
    service = Service(tmp_path, node="node-b", pki=pki, primary=False)
    service.coordinator.primary = lambda: None
    with pytest.raises(AgentError) as refused:
        ClusterChannel(service).forward("POST", "/preview", {})
    assert (refused.value.code, refused.value.status) == ("no_primary", 503)


def test_keygen_writes_a_private_secret_once(tmp_path):
    target = tmp_path / "cluster.secret"
    assert cluster_channel.write_secret(str(target))["state"] == "written"
    assert len(target.read_text().strip()) == 64
    with pytest.raises(AgentError, match="cluster_secret_exists"):
        cluster_channel.write_secret(str(target))
