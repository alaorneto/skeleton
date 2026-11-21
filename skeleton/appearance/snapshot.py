"""What every page reads of the instance appearance (#592), cached per process.

``current_appearance()`` reads the single brand row (and the active theme) at most every
``TTL_S`` seconds; a database error keeps the last good value for ``STALE_S`` seconds, then
falls back to the core appearance. Every write calls ``reset_appearance_cache()`` after its
commit, so the process that writes sees the change at once and the others within the TTL.
``cached_appearance()`` never touches the database: it is what the error pages use.
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

    if not apps.ready:
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


def _load() -> Appearance:
    from .models import InstanceBrand
    from .themes import BUILTIN_THEMES, ThemeValues, theme_css, theme_css_tokens, theme_digest

    brand = InstanceBrand.objects.select_related("active_theme").filter(slot="instance").first()
    if brand is None:
        return DEFAULT
    values: ThemeValues | None = None
    theme = brand.active_theme
    if theme is not None and theme.deleted_at is None:
        values = ThemeValues(theme.name, theme.brand_color, theme.radius, theme.font, theme.custom_css)
    elif brand.builtin_theme:
        values = BUILTIN_THEMES.get(brand.builtin_theme)  # An unknown slug is the core theme, never an error.
    css = theme_css(values) if values is not None else ""
    tokens = theme_css_tokens(values) if values is not None else ""
    return Appearance(
        product_name=brand.product_name,
        footer_text=brand.footer_text,
        logo_version=_version(brand.logo_key),
        logo_dark_version=_version(brand.logo_dark_key),
        icon_version=_version(brand.icon_key),
        theme_css=css,
        theme_css_tokens=tokens,
        theme_version=theme_digest(css),
        theme_safe_version=theme_digest(tokens),
        brand_color=values.brand_color.lower() if values is not None else "",
    )
