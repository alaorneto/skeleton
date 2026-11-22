"""The official publisher's public key is versioned (#656): the agent image approves only it."""

from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

ROOT = Path(__file__).resolve().parents[2]
KEYS = ROOT / "deploy" / "release" / "publishers"


def test_only_the_official_ed25519_public_key_is_versioned():
    assert sorted(path.name for path in KEYS.iterdir()) == ["skeleton.pub.pem"]
    data = (KEYS / "skeleton.pub.pem").read_bytes()
    assert b"PRIVATE" not in data
    assert isinstance(load_pem_public_key(data), Ed25519PublicKey)


def test_the_agent_image_ships_the_key_where_the_default_configuration_reads_it():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY deploy/release/publishers /opt/agent/publishers" in dockerfile
    source = (ROOT / "deployment_agent" / "bootstrap.py").read_text(encoding="utf-8")
    assert 'OFFICIAL_PUBLISHER_KEY = "/opt/agent/publishers/skeleton.pub.pem"' in source
