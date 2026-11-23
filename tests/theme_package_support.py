"""Builders of theme packages for the tests of epic #629 (format of #633): zips and directories in memory."""

from __future__ import annotations

import io
import json
import struct
import zipfile
import zlib
from pathlib import Path
from typing import Any

NS = 'xmlns="http://www.w3.org/2000/svg"'
LOGO_SVG = f'<svg {NS} viewBox="0 0 120 40"><title>Aurora</title><rect width="120" height="40" fill="#1d4ed8"/></svg>'
FONT = b"wOF2" + b"\x00" * 252


def png(width: int = 512, height: int = 512, grey: int = 0x80) -> bytes:
    """A real PNG of the given size, one grey value, built in memory."""
    raw = b"".join(b"\x00" + bytes([grey]) * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def manifest(**changes: Any) -> dict[str, Any]:
    """The full example manifest of docs/appearance.md; ``key=None`` removes a key."""
    value: dict[str, Any] = {
        "format": 1,
        "id": "org.example.theme.aurora",
        "version": "1.0.0",
        "name": "Aurora",
        "description": "Tema da marca Aurora.",
        "publisher": "org.example",
        "compatibility": {"skeleton": ">=0.0.0"},
        "tokens": {"brand_color": "#1d4ed8", "radius": "round", "font": "default", "font_family": "Aurora Sans"},
        "brand": {"product_name": "Aurora", "footer_text": "Aurora · atendimento ao cliente"},
        "images": {"logo": "images/logo.svg", "logo_dark": "images/logo-dark.png", "icon": "images/icon.png"},
        "fonts": [{"family": "Aurora Sans", "file": "fonts/aurora-sans.woff2", "weight": "100 900", "style": "normal"}],
        "css": "theme.css",
    }
    for key, change in changes.items():
        if change is None:
            value.pop(key, None)
        else:
            value[key] = change
    return value


def files(
    changes: dict[str, bytes | None] | None = None, *, manifest_value: dict[str, Any] | None = None
) -> dict[str, bytes]:
    """Every file of the example package; ``changes`` adds or (with ``None``) removes a path."""
    content: dict[str, bytes] = {
        "theme.json": json.dumps(manifest_value or manifest(), ensure_ascii=False).encode(),
        "theme.css": b':root{--acme-accent:#2bb673}\n.page-header h1{font-family:"Aurora Sans"}\n',
        "images/logo.svg": LOGO_SVG.encode(),
        "images/logo-dark.png": png(64, 32, 0xF0),
        "images/icon.png": png(),
        "fonts/aurora-sans.woff2": FONT,
        "README.md": b"# Aurora\n",
        "LICENSES/OFL.txt": b"SIL Open Font License\n",
    }
    for path, value in (changes or {}).items():
        if value is None:
            content.pop(path, None)
        else:
            content[path] = value
    return content


def make_zip(content: dict[str, bytes], *, folder: str = "") -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, data in content.items():
            archive.writestr(folder + path, data)
    return stream.getvalue()


def theme_zip(changes: dict[str, bytes | None] | None = None, **kwargs: Any) -> bytes:
    return make_zip(files(changes, **kwargs))


def write_directory(root: Path, content: dict[str, bytes]) -> Path:
    for path, data in content.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return root


APP_THEME_ID = "org.example.demo"


def app_theme_manifest(**changes: Any) -> dict[str, Any]:
    """The example manifest as the theme ``aurora`` of the app ``org.example.demo`` (epic #630)."""
    values = {"id": f"{APP_THEME_ID}.theme.aurora", "compatibility": {"skeleton": ">=0.0.0"}}
    return manifest(**{**values, **changes})


def register_theme_app(root: Path, *, label: str = "theme_demo", themes: dict[str, dict[str, bytes]] | None = None):
    """Register an in-memory app that ships ``themes`` (slug → files) written under ``root``.

    Returns the manifest; pair with ``unregister_theme_app(label)`` in a ``finally`` or fixture.
    """
    from skeleton.system.app_registry import AppManifest, AppThemeDeclaration, register_app

    declarations = []
    for slug, content in (themes or {"aurora": files(manifest_value=app_theme_manifest())}).items():
        directory = write_directory(root / label / "themes" / slug, content)
        declarations.append(AppThemeDeclaration(slug, str(directory.resolve())))
    return register_app(
        AppManifest(
            app_label=label,
            label="Demo de temas",
            description="Fixture",
            app_id=APP_THEME_ID if label == "theme_demo" else f"org.example.{label}",
            version="1.2.0",
            instance_themes=tuple(declarations),
        )
    )


def unregister_theme_app(label: str = "theme_demo") -> None:
    from skeleton.system.app_registry import unregister_app

    unregister_app(label)
    try:
        from skeleton.appearance.app_themes import reset_app_themes_cache
    except ImportError:  # Before #643.
        return
    reset_app_themes_cache()
