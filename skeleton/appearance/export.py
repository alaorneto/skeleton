"""Export any theme of the catalog as a theme package (#638): the same format the installation reads.

A package theme comes back with its stored ``theme.json`` and files, so without the brand the
``content_digest`` is the one of the package that was installed. A form or built-in theme gets a
local id (``local.theme.<slug>``, version ``1.0.0``, the current core line as compatibility) that
the author changes before publishing. With ``include_brand`` the zip also carries the product
name, the footer and the images the administrator uploaded, which win over the theme's own.

The zip is read back with the package reader before it is returned: a theme the installation
would refuse today (an old CSS, for instance) is never exported.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from .package_build import build_theme_zip
from .package_format import ThemePackageError, canonical_font, read_theme_archive
from .package_messages import describe
from .services import _require_platform

__all__ = ["export_theme_package"]

EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}


def _slug(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")[:60].strip("-")
    return slug or "tema"


def _compatibility() -> str:
    from packaging.version import Version

    from skeleton.versioning import core_version

    version = core_version()["version"]
    if not version or Version(version) == Version("0.0.0"):
        return ">=0.0.0"
    parsed = Version(version)
    return f">={parsed.major}.{parsed.minor}.0,<{parsed.major}.{parsed.minor + 1}.0"


def _base_manifest(values: Any, slug: str) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "format": 1,
        "id": f"local.theme.{slug}",
        "version": "1.0.0",
        "name": values.name,
        "compatibility": {"skeleton": _compatibility()},
        "tokens": {
            "brand_color": values.brand_color,
            "radius": values.radius,
            "font": canonical_font(values.font),
            "font_family": "",
        },
    }
    return manifest


def _package_files(theme: Any) -> tuple[dict[str, Any], dict[str, bytes]]:
    """``(manifest, files)`` of an installed package theme, read back from storage."""
    from .assets import open_asset, package_assets

    manifest = dict(theme.package_manifest or {})
    files: dict[str, bytes] = {}
    stored = theme.package_files or {}
    refs = {ref.name: ref for ref in package_assets(theme)}
    for kind in ("logo", "logo_dark", "icon"):
        entry = stored.get(kind)
        if isinstance(entry, dict):
            content = open_asset(refs.get(kind))
            if content is None:
                raise _missing()
            files[entry["file"]] = content
    for entry in stored.get("fonts", []) or []:
        content = open_asset(refs.get(entry["file"].rsplit("/", 1)[-1]))
        if content is None:
            raise _missing()
        files[entry["file"]] = content
    return manifest, files


def _missing() -> Exception:
    from django.core.exceptions import ValidationError
    from django.utils.translation import gettext

    return ValidationError(gettext("Um arquivo do pacote não está mais no armazenamento; envie o pacote de novo."))


def _with_brand(manifest: dict[str, Any], files: dict[str, bytes]) -> None:
    """The uploaded brand of the instance wins over the theme's own name, footer and images."""
    from .images import open_image
    from .services import current_brand

    brand = current_brand()
    if brand is None:
        return
    names = dict(manifest.get("brand") or {"product_name": "", "footer_text": ""})
    if brand.product_name:
        names["product_name"] = brand.product_name
    if brand.footer_text:
        names["footer_text"] = brand.footer_text
    manifest["brand"] = names
    images = dict(manifest.get("images") or {})
    if brand.logo_key:
        for kind in ("logo", "logo_dark"):  # The dark logo always comes from the same origin as the logo.
            old = images.pop(kind, None)
            if old:
                files.pop(old, None)
    for kind, field in (("logo", "logo_key"), ("logo_dark", "logo_dark_key"), ("icon", "icon_key")):
        if not getattr(brand, field):
            continue
        found = open_image(kind)
        if found is None:
            continue
        content, mime = found
        old = images.pop(kind, None)
        if old:
            files.pop(old, None)
        path = "images/icon.png" if kind == "icon" else f"images/{kind.replace('_', '-')}.{EXTENSIONS[mime]}"
        images[kind] = path
        files[path] = content
    manifest["images"] = images


def export_theme_package(*, actor: Any, builtin: str = "", theme_id: Any = None, include_brand: bool = False):
    """``(file name, zip bytes)`` of a theme of the catalog; ``ValidationError`` if it cannot be exported."""
    import json

    from django.core.exceptions import ValidationError
    from django.utils.translation import gettext

    from .services import get_theme
    from .themes import BUILTIN_THEMES

    _require_platform(actor)
    manifest: dict[str, Any]
    files: dict[str, bytes]
    css: str
    if bool(builtin) == (theme_id is not None):
        raise ValidationError(gettext("Escolha um tema só."))
    if builtin:
        values = BUILTIN_THEMES.get(builtin)
        if values is None:
            raise ValidationError(gettext("Tema pronto inexistente."))
        manifest, files, css = _base_manifest(values, builtin), {}, ""
    else:
        theme = get_theme(theme_id)
        if theme is None:
            raise ValidationError(gettext("Tema inexistente."))
        if theme.source == "package":
            manifest, files = _package_files(theme)
            css = theme.custom_css
        else:
            manifest, files, css = _base_manifest(theme, _slug(theme.name)), {}, theme.custom_css
    if css.strip():
        name = manifest.get("css") or "theme.css"
        manifest["css"] = name
        files[name] = css.encode("utf-8")
    else:
        manifest.pop("css", None)
    if include_brand:
        _with_brand(manifest, files)
    files["theme.json"] = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    data = build_theme_zip(files)
    try:
        package = read_theme_archive(data)
    except ThemePackageError as error:
        raise ValidationError(describe(error)) from None
    safe = re.sub(r"[^a-z0-9._-]", "-", f"{package.id}-{package.version}".lower())
    return f"{safe}.zip", data
