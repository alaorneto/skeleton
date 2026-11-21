"""What every page reads of the instance appearance (#592), cached per process.

``current_appearance()`` reads the single brand row (and the active theme) at most every
``TTL_S`` seconds; a database error keeps the last good value for ``STALE_S`` seconds, then
falls back to the core appearance. Every write calls ``reset_appearance_cache()`` after its
commit, so the process that writes sees the change at once and the others within the TTL.
``cached_appearance()`` never touches the database: it is what the error pages use. Setup
mode, which has no database, always answers the core appearance.
``pin_appearance()`` fixes the value in tests (``tests/conftest.py`` pins the default).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("skeleton.appearance")

TTL_S = 5.0
STALE_S = 300.0


@dataclass(frozen=True, slots=True)
class Appearance:
    """Immutable presentation of the instance brand; empty strings mean "the core default"."""

    product_name: str = ""
    footer_text: str = ""
    logo_version: str = ""
    logo_dark_version: str = ""
    icon_version: str = ""
    theme_css: str = ""
    theme_css_tokens: str = ""
    theme_version: str = ""
    theme_safe_version: str = ""
    brand_color: str = ""
    # Where the logo (and its dark variant) and the icon come from (#636): "brand" (uploaded by the
    # administrator), "theme" (the active theme package) or "" (none). The dark logo always comes
    # from the same origin as the logo, never mixing two different logos.
    logo_source: str = ""
    icon_source: str = ""
    logo_mime: str = ""
    theme_assets: tuple[Any, ...] = ()  # AssetRef of the active theme (images and fonts).

    def theme_asset(self, name: str) -> Any:
        return next((ref for ref in self.theme_assets if ref.name == name), None)


DEFAULT = Appearance()
_CACHE: dict[str, Any] = {"value": None, "at": 0.0, "good": None, "good_at": 0.0, "pinned": None}


def reset_appearance_cache() -> None:
    """Forget the cached value (after a write commits, and in tests); a pin stays."""
    _CACHE.update(value=None, at=0.0, good=None, good_at=0.0)


def pin_appearance(value: Appearance | None) -> None:
    """Tests only: always answer ``value`` (``None`` removes the pin)."""
    _CACHE["pinned"] = value


def cached_appearance() -> Appearance:
    """The value in memory, or ``DEFAULT``; never a query (500 pages render with the database down)."""
    if _CACHE["pinned"] is not None:
        return _CACHE["pinned"]
    value = _CACHE["value"] or _CACHE["good"]
    return value if value is not None else DEFAULT


def current_appearance() -> Appearance:
    """The appearance of the instance, read at most every ``TTL_S`` seconds per process."""
    if _CACHE["pinned"] is not None:
        return _CACHE["pinned"]
    from django.apps import apps
    from django.conf import settings

    # Setup mode runs with the dummy database backend (#146): its pages keep the core theme.
    if not apps.ready or getattr(settings, "SKELETON_SETUP_MODE", False):
        return DEFAULT
    now = time.monotonic()
    if _CACHE["value"] is not None and now - _CACHE["at"] < TTL_S:
        return _CACHE["value"]
    from django.db import DatabaseError

    try:
        value = _load()
    except DatabaseError:
        logger.warning("appearance.unavailable")  # Never a name, a colour, a key or CSS in logs.
        if _CACHE["good"] is not None and now - _CACHE["good_at"] < STALE_S:
            return _CACHE["good"]
        return DEFAULT
    _CACHE.update(value=value, at=now, good=value, good_at=now)
    return value


def _version(key: str) -> str:
    return key[-12:] if key else ""


def _public_font_url(ref: Any) -> str:
    return f"/brand/fonts/{ref.name}?v={ref.version}"


def _load() -> Appearance:
    from django.db import transaction

    from .assets import package_assets, package_theme_values
    from .models import InstanceBrand
    from .themes import BUILTIN_THEMES, ThemeValues, theme_css, theme_css_tokens, theme_digest

    # A savepoint: a failed read never breaks the transaction of the request that renders the page.
    with transaction.atomic():
        brand = InstanceBrand.objects.select_related("active_theme").filter(slot="instance").first()
    if brand is None:
        return DEFAULT
    values: ThemeValues | None = None
    theme = brand.active_theme
    if theme is not None and theme.deleted_at is not None:
        theme = None
    if theme is not None:
        values = package_theme_values(theme, font_url=_public_font_url)
    elif brand.builtin_theme:
        values = BUILTIN_THEMES.get(brand.builtin_theme)  # An unknown slug is the core theme, never an error.
    assets = package_assets(theme)
    by_name = {ref.name: ref for ref in assets}
    css = theme_css(values) if values is not None else ""
    tokens = theme_css_tokens(values) if values is not None else ""
    logo = dict(source="", version="", dark="", mime="")
    if brand.logo_key:
        logo.update(source="brand", version=_version(brand.logo_key), dark=_version(brand.logo_dark_key))
    elif "logo" in by_name:
        dark = by_name.get("logo_dark")
        logo.update(
            source="theme",
            version=by_name["logo"].version,
            dark=dark.version if dark is not None else "",
            mime=by_name["logo"].mime,
        )
    icon_source, icon_version = "", ""
    if brand.icon_key:
        icon_source, icon_version = "brand", _version(brand.icon_key)
    elif "icon" in by_name:
        icon_source, icon_version = "theme", by_name["icon"].version
    from_theme = theme is not None and theme.source == "package"
    return Appearance(
        product_name=brand.product_name or (theme.product_name if from_theme else ""),
        footer_text=brand.footer_text or (theme.footer_text if from_theme else ""),
        logo_version=logo["version"],
        logo_dark_version=logo["dark"],
        icon_version=icon_version,
        theme_css=css,
        theme_css_tokens=tokens,
        theme_version=theme_digest(css),
        theme_safe_version=theme_digest(tokens),
        brand_color=values.brand_color.lower() if values is not None else "",
        logo_source=logo["source"],
        icon_source=icon_source,
        logo_mime=logo["mime"],
        theme_assets=assets,
    )
