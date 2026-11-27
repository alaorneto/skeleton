"""Every vendored asset and editor bundle chunk is recorded in ``VENDOR.json`` with its SHA-256 (#911)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC_ROOT = ROOT / "skeleton" / "web" / "static" / "skeleton" / "web"
MANIFEST = STATIC_ROOT / "vendor" / "VENDOR.json"


def _digest(path: Path) -> str:
    raw = path.read_bytes()
    if path.suffix != ".woff2":
        raw = raw.replace(b"\r\n", b"\n")
    return hashlib.sha256(raw).hexdigest()


def test_vendor_manifest_lists_every_vendor_and_editor_file_with_matching_sha256():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    listed: dict[str, str] = manifest["files"]

    vendor_files = [
        path.relative_to(STATIC_ROOT).as_posix()
        for path in sorted((STATIC_ROOT / "vendor").rglob("*"))
        if path.is_file() and path != MANIFEST
    ]
    editor_files = [
        path.relative_to(STATIC_ROOT).as_posix()
        for path in sorted(STATIC_ROOT.glob("markdown_editor*"))
        if path.is_file()
    ]
    expected = sorted({*vendor_files, *editor_files})

    assert sorted(listed) == expected
    assert any(name.startswith("markdown_editor-") for name in listed), "split chunks must be indexed"
    for name in expected:
        assert listed[name] == _digest(STATIC_ROOT / name), name
