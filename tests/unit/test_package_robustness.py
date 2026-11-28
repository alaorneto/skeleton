"""Package artifacts fail closed on corruption, limits and platform, on every OS (#255).

Unlike tests/unit/test_packages.py these run without POSIX: they exercise the helpers directly.
"""

import io
import json
import os
import zipfile

import pytest

from skeleton.system import packages
from skeleton.system.packages import PackageError
from skeleton.system.packaging import limits


def _zip(members, *, compression=zipfile.ZIP_DEFLATED):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=compression) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def test_corrupt_member_is_a_package_error():
    raw = bytearray(_zip({"app/module.py": b"VALUE = 1\n" * 200}))
    with zipfile.ZipFile(io.BytesIO(bytes(raw))) as archive:
        info = archive.getinfo("app/module.py")
        start = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
    raw[start : start + 8] = b"\xff" * 8  # Corrupt the deflate stream.
    with zipfile.ZipFile(io.BytesIO(bytes(raw))) as archive:
        with pytest.raises(PackageError, match="Corrupt archive member|size mismatch"):
            packages._read_member(archive, archive.getinfo("app/module.py"))


def test_member_limit_is_read_at_call_time(monkeypatch):
    with zipfile.ZipFile(io.BytesIO(_zip({"a": b"1", "b": b"2"}))) as archive:
        monkeypatch.setattr(limits, "MAX_FILE_BYTES", 0)
        with pytest.raises(PackageError, match="size limit"):
            packages._read_member(archive, archive.getinfo("a"))


def test_one_wheel_may_exceed_the_artifact_member_limit(monkeypatch):
    members = {f"pkg/module_{index}.py": b"" for index in range(6)}
    with zipfile.ZipFile(io.BytesIO(_zip(members))) as archive:
        monkeypatch.setattr(limits, "MAX_MEMBERS", 4)
        with pytest.raises(PackageError, match="too many members"):
            packages._zip_members(archive)
        monkeypatch.setattr(limits, "MAX_WHEEL_MEMBERS", 10)
        assert len(packages._zip_members(archive, limits.MAX_WHEEL_MEMBERS)) == 6
        monkeypatch.setattr(limits, "MAX_WHEEL_MEMBERS", 4)
        with pytest.raises(PackageError, match="too many members"):
            packages._zip_members(archive, limits.MAX_WHEEL_MEMBERS)


def test_snapshot_without_nofollow_fails_closed(monkeypatch, tmp_path):
    artifact = tmp_path / "artifact.skapp"
    artifact.write_bytes(b"not read")
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    with pytest.raises(PackageError, match="POSIX filesystem"):
        packages._protected_snapshot(artifact)


def test_trust_store_paths_resolve_against_the_store(tmp_path):
    store = tmp_path / "config" / "trust.json"
    store.parent.mkdir()
    store.write_text(
        json.dumps(
            {
                "org.example": "keys/publisher.pem",
                "org.inline": "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n",
                "org.dict": {"public_key": "keys/other.pem", "publisher": "org.dict"},
            }
        )
    )
    trust = packages.load_trust_store(store)
    assert trust["org.example"] == str(store.parent / "keys/publisher.pem")
    assert trust["org.inline"].startswith("-----BEGIN")
    assert trust["org.dict"]["public_key"] == str(store.parent / "keys/other.pem")


def test_trust_store_must_be_a_mapping_and_small(tmp_path, monkeypatch):
    store = tmp_path / "trust.json"
    store.write_text("[]")
    with pytest.raises(PackageError):
        packages.load_trust_store(store)
    store.write_text("{}")
    monkeypatch.setattr(limits, "MAX_METADATA_BYTES", 1)
    with pytest.raises(PackageError, match="too large"):
        packages.load_trust_store(store)


def test_core_commands_share_the_trust_store_loader():
    from skeleton import distribution
    from skeleton.system.management.commands import adopt_core_distribution

    assert distribution.load_trust_store is packages.load_trust_store
    assert adopt_core_distribution.load_trust_store is packages.load_trust_store


def test_packaging_modules_import_without_django():
    """The CLI and the agent import these without settings, on every OS (#254, #310 regression)."""
    import subprocess
    import sys

    code = (
        "import sys; import skeleton.system.dependencies, skeleton.system.packages; "
        "assert 'django' not in sys.modules, sorted(m for m in sys.modules if m.startswith('django'))[:3]"
    )
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)


def _forged_zip_bomb(member_name: str, *, real_bytes: int = 64 * 1024 * 1024, claimed_size: int = 100) -> bytes:
    """Create a DEFLATE zip whose central directory and local header lie about ``file_size`` (#891)."""
    import struct

    raw = bytearray(_zip({member_name: b"\x00" * real_bytes}, compression=zipfile.ZIP_DEFLATED))
    # Patch local file header uncompressed size (offset 22) and central directory header (offset 24).
    local_pos = raw.index(b"PK\x03\x04")
    central_pos = raw.index(b"PK\x01\x02")
    packed = struct.pack("<I", claimed_size)
    raw[local_pos + 22 : local_pos + 26] = packed
    raw[central_pos + 24 : central_pos + 28] = packed
    return bytes(raw)


def test_zip_bomb_with_forged_file_size_fails_bounded_in_memory():
    """#891: a 64 MiB zero member with forged file_size=100 is rejected without inflating into RAM."""
    import tracemalloc

    from skeleton.system.packaging.zipio import read_bounded

    bomb = _forged_zip_bomb("manifest.json", real_bytes=64 * 1024 * 1024, claimed_size=100)
    limit = 256 * 1024
    tracemalloc.start()
    try:
        with zipfile.ZipFile(io.BytesIO(bomb)) as archive:
            info = archive.getinfo("manifest.json")
            with pytest.raises(PackageError):
                read_bounded(archive, info, limit)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < limit + 1024 * 1024


def test_read_bounded_rejects_unsupported_compression_and_expansion_ratio():
    """#891: compression other than STORED/DEFLATED and excessive real expansion are rejected."""
    from skeleton.system.packaging.zipio import read_bounded

    for method in (zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA):
        raw = _zip({"manifest.json": b"{}"}, compression=method)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            with pytest.raises(PackageError, match="unsupported compression"):
                read_bounded(archive, archive.getinfo("manifest.json"), 1024)

    # Real expansion ratio check over 256 KiB of zeros compressed to ~300 bytes (> 100x).
    compressible = _zip({"manifest.json": b"\x00" * (256 * 1024)}, compression=zipfile.ZIP_DEFLATED)
    with zipfile.ZipFile(io.BytesIO(compressible)) as archive:
        with pytest.raises(PackageError, match="expansion limit"):
            read_bounded(archive, archive.getinfo("manifest.json"), 512 * 1024, max_expansion_ratio=10)
