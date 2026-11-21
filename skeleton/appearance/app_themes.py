"""Instance themes shipped by installed apps (epic #630).

An app declares theme directories in ``runtime.instance_themes`` of its manifest (#642). A theme of
an app is never copied to the database: it exists exactly while the code of the app is installed,
as the built-in themes exist while the code of the core is. Uninstalling the app (the deployment
agent swaps the code and restarts the processes) takes the theme out of the catalog with no
synchronization step.

``check_declared_theme`` reads one directory with the package reader and applies the rules the
installation of a zip applies (AA contrast, core range) plus the id rule of app themes
(``<app_id>.theme.<slug>``). It answers a package or a Portuguese message, never raises.
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from typing import Any

__all__ = [
    "AppTheme",
    "app_theme_assets",
    "app_theme_values",
    "app_themes",
    "check_declared_theme",
    "declared_theme_problems",
    "expected_theme_id",
    "get_app_theme",
    "reset_app_themes_cache",
]

logger = logging.getLogger("skeleton.appearance")
_CACHE: dict[str, Any] = {"themes": None}
_WARNED: set[str] = set()


@dataclass(frozen=True, slots=True)
class AppTheme:
    """A valid theme of an installed app; ``package`` keeps the metadata, never the bytes of files."""

    key: str  # "<app_label>:<slug>"
    app_label: str
    app_name: str
    app_version: str
    slug: str
    directory: str  # Never shown, logged or put in a URL.
    package: Any


def expected_theme_id(manifest: Any, slug: str) -> str:
    from skeleton.system.packaging.themes import theme_id

    return theme_id(manifest.app_id or manifest.app_label, slug)


def check_declared_theme(manifest: Any, declaration: Any) -> tuple[Any, str]:
    """``(package, "")`` for a valid theme, ``(None, message)`` otherwise."""
    from django.core.exceptions import ValidationError
    from django.utils.translation import gettext

    from skeleton.tenancy.branding import validate_brand_color
    from skeleton.versioning import core_version

    from .package_format import ThemePackageError, check_core_compatibility, read_theme_directory
    from .package_messages import describe, package_error_message

    try:
        package = read_theme_directory(declaration.path)
        check_core_compatibility(package, core_version()["version"])
    except ThemePackageError as error:
        return None, describe(error)
    expected = expected_theme_id(manifest, declaration.slug)
    if package.id != expected:
        return None, gettext("o id do theme.json precisa ser %(id)s.") % {"id": expected}
    if package.brand_color:
        try:
            validate_brand_color(package.brand_color)
        except ValidationError:
            return None, package_error_message("brand_color")
    return package, ""


def declared_theme_problems(manifest: Any) -> list[tuple[str, str]]:
    """``[(slug, message)]`` of the invalid themes an app declares (check E047)."""
    problems = []
    for declaration in manifest.instance_themes:
        package, message = check_declared_theme(manifest, declaration)
        if package is None:
            problems.append((declaration.slug, message))
    return problems


def _without_bytes(package: Any) -> Any:
    """The package with empty contents: the registry keeps metadata only (sha256, file, mime)."""
    images = tuple(dataclasses.replace(image, content=b"") for image in package.images)
    fonts = tuple(dataclasses.replace(font, content=b"") for font in package.fonts)
    return dataclasses.replace(package, images=images, fonts=fonts)


def _build() -> dict[str, AppTheme]:
    from skeleton.system.app_registry import all_apps

    themes: dict[str, AppTheme] = {}
    for manifest in all_apps():
        for declaration in manifest.instance_themes:
            package, _message = check_declared_theme(manifest, declaration)
            key = f"{manifest.app_label}:{declaration.slug}"
            if package is None:
                # E047 reports the problem; the log carries only the app label (no path, no value).
                logger.warning("appearance.app_theme_invalid", extra={"app_label": manifest.app_label})
                continue
            themes[key] = AppTheme(
                key,
                manifest.app_label,
                manifest.label,
                manifest.version,
                declaration.slug,
                declaration.path,
                _without_bytes(package),
            )
    return themes


def app_themes() -> dict[str, AppTheme]:
    """The valid themes of the installed apps, built once per process (code never changes at runtime)."""
    if _CACHE["themes"] is None:
        _CACHE["themes"] = _build()
    themes: dict[str, AppTheme] = _CACHE["themes"]
    return themes


def get_app_theme(key: Any) -> AppTheme | None:
    """The theme of ``"<app_label>:<slug>"``, or ``None`` (unknown, invalid or the app was uninstalled)."""
    if not isinstance(key, str) or not key:
        return None
    theme = app_themes().get(key)
    if theme is None and key not in _WARNED:
        _WARNED.add(key)  # At most once per process and key, never on every request.
        logger.warning("appearance.app_theme_missing")
    return theme


def reset_app_themes_cache() -> None:
    """Forget the registry (tests, and after registering or unregistering an app in a test)."""
    _CACHE["themes"] = None
    _WARNED.clear()


def app_theme_assets(theme: AppTheme | None) -> tuple[Any, ...]:
    """``AssetRef`` of the images and fonts of an app theme (locator ``app:<label>:<slug>:<file>``)."""
    from .assets import AssetRef, font_name

    if theme is None:
        return ()
    refs = [
        AssetRef(image.kind, image.mime, image.sha256, f"app:{theme.key}:{image.file}")
        for image in theme.package.images
    ]
    refs += [
        AssetRef(font_name(font.file), "font/woff2", font.sha256, f"app:{theme.key}:{font.file}")
        for font in theme.package.fonts
    ]
    return tuple(refs)


def app_theme_values(theme: AppTheme, *, font_url: Any) -> Any:
    """``ThemeValues`` of an app theme, with its fonts served at ``font_url(ref)``."""
    from .assets import font_name
    from .themes import ThemeFont, ThemeValues

    package = theme.package
    refs = {ref.name: ref for ref in app_theme_assets(theme) if ref.mime == "font/woff2"}

    fonts = tuple(
        ThemeFont(font.family, font.weight, font.style, font_url(refs[font_name(font.file)])) for font in package.fonts
    )
    return ThemeValues(
        package.name,
        package.brand_color,
        package.radius,
        package.font,
        package.custom_css,
        font_family=package.font_family,
        fonts=fonts,
    )
