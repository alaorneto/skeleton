"""A deterministic zip of a theme package (#634): the same files always give the same bytes.

Only what the package uses goes in (``theme.json``, the CSS, the declared images and fonts,
``README.md`` and ``LICENSES/*.txt``), in alphabetical order, with a fixed date and mode and
no directory entries. Pure Python: no Django, no database.
"""

from __future__ import annotations

import io
import zipfile
from typing import Any

from .package_format import ThemePackage

__all__ = ["build_theme_zip", "package_files"]

FIXED_DATE = (1980, 1, 1, 0, 0, 0)


def package_files(package: ThemePackage, source: dict[str, bytes]) -> dict[str, bytes]:
    """The files of ``source`` that belong in the zip of ``package``."""
    used = {"theme.json"}
    if package.manifest.get("css"):
        used.add(package.manifest["css"])
    used |= {image.file for image in package.images} | {font.file for font in package.fonts}
    chosen = {path: data for path, data in source.items() if path in used}
    chosen |= {path: data for path, data in source.items() if path == "README.md" or path.startswith("LICENSES/")}
    return chosen


def build_theme_zip(files: dict[str, Any]) -> bytes:
    """The zip of ``files`` (path → bytes), always the same bytes for the same files."""
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            info = zipfile.ZipInfo(path, date_time=FIXED_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            info.create_system = 3
            archive.writestr(info, bytes(files[path]))
    return stream.getvalue()
