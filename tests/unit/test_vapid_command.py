"""generate_vapid_keys writes a P-256 PEM the push channel loads and prints its public key (#335)."""

import io
import stat

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from skeleton.notifications.channels import push


def test_generated_key_loads_and_matches_the_printed_public_key(tmp_path):
    path = tmp_path / "vapid.pem"
    out = io.StringIO()
    call_command("generate_vapid_keys", str(path), stdout=out)
    printed = out.getvalue().strip().splitlines()[-1]
    key = push._load_key(str(path), path.stat().st_mtime)
    assert key.public == printed
    assert len(push.b64url_decode(printed)) == 65  # Uncompressed P-256 point.


def test_existing_file_is_never_overwritten(tmp_path):
    path = tmp_path / "vapid.pem"
    path.write_bytes(b"keep")
    with pytest.raises(CommandError):
        call_command("generate_vapid_keys", str(path), stdout=io.StringIO())
    assert path.read_bytes() == b"keep"


@pytest.mark.posix
def test_private_key_file_is_0600(tmp_path):
    path = tmp_path / "vapid.pem"
    call_command("generate_vapid_keys", str(path), stdout=io.StringIO())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
