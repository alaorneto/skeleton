"""The files of the active theme (#636): images and fonts a theme package brought.

An ``AssetRef`` names a file of a theme without saying where it lives: ``locator`` is
``storage:<key>`` for an installed package (``package_files``), and epic #630 adds the files of
an app theme. The locator never goes into a URL, a log or a page: the public routes take only the
kind (``logo``, ``logo_dark``, ``icon``) or the file name of a declared font, and ``?v=`` is the
first 12 digits of the SHA-256 of the content, never a key.

``open_asset`` reads the bytes within the limit of the type and checks the full SHA-256; any
failure is ``None`` (the route answers 404, never 500).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

__all__ = ["AssetRef", "font_name", "open_asset", "package_assets", "package_theme_values"]

LIMITS = {
    "image/png": 512 * 1024,
    "image/jpeg": 512 * 1024,
    "image/webp": 512 * 1024,
    "image/svg+xml": 256 * 1024,
    "font/woff2": 512 * 1024,
}
ICON_LIMIT = 256 * 1024


@dataclass(frozen=True, slots=True)
class AssetRef:
    name: str  # "logo" | "logo_dark" | "icon" | the file name of a font ("aurora-sans.woff2")
    mime: str
    sha256: str
    locator: str

    @property
    def version(self) -> str:
        return self.sha256[:12]


def font_name(file: str) -> str:
    """The URL name of a font file of a package (``fonts/aurora-sans.woff2`` → ``aurora-sans.woff2``)."""
    return file.rsplit("/", 1)[-1]


def package_assets(theme: Any) -> tuple[AssetRef, ...]:
    """The images and fonts of an installed package theme; ``()`` for any other theme."""
    if theme is None or getattr(theme, "source", "") != "package":
        return ()
    files = theme.package_files or {}
    refs = []
    for kind in ("logo", "logo_dark", "icon"):
        entry = files.get(kind)
        if isinstance(entry, dict) and entry.get("key") and entry.get("sha256") and entry.get("mime") in LIMITS:
            refs.append(AssetRef(kind, entry["mime"], entry["sha256"], f"storage:{entry['key']}"))
    for entry in files.get("fonts", []) or []:
        if isinstance(entry, dict) and entry.get("key") and entry.get("sha256") and entry.get("file"):
            refs.append(AssetRef(font_name(entry["file"]), "font/woff2", entry["sha256"], f"storage:{entry['key']}"))
    return tuple(refs)


def package_theme_values(theme: Any, *, font_url: Any) -> Any:
    """``ThemeValues`` of a catalog theme; a package theme also gets its fonts (``font_url(ref)``)."""
    from .themes import ThemeFont, ThemeValues

    fonts: tuple[ThemeFont, ...] = ()
    if getattr(theme, "source", "") == "package":
        refs = {ref.name: ref for ref in package_assets(theme) if ref.mime == "font/woff2"}
        entries = (theme.package_files or {}).get("fonts", []) or []
        fonts = tuple(
            ThemeFont(entry["family"], entry["weight"], entry["style"], font_url(refs[font_name(entry["file"])]))
            for entry in entries
            if isinstance(entry, dict) and font_name(entry.get("file", "")) in refs
        )
    return ThemeValues(
        theme.name,
        theme.brand_color,
        theme.radius,
        theme.font,
        theme.custom_css,
        font_family=getattr(theme, "font_family", "") or "",
        fonts=fonts,
    )


def open_asset(ref: AssetRef | None) -> bytes | None:
    """The bytes of ``ref``, or ``None`` when missing, too large or not the content recorded."""
    if ref is None:
        return None
    limit = ICON_LIMIT if ref.name == "icon" else LIMITS.get(ref.mime, 0)
    content = _read(ref.locator, limit)
    if content is None or len(content) > limit or hashlib.sha256(content).hexdigest() != ref.sha256:
        return None
    return content


def _read(locator: str, limit: int) -> bytes | None:
    kind, _, rest = locator.partition(":")
    if kind == "storage":
        from skeleton.files.storage import StorageError, open_object

        try:
            with open_object(rest) as stream:
                data: bytes = stream.read(limit + 1)
                return data
        except (StorageError, FileNotFoundError, OSError, ValueError):
            return None
    return None
