"""Presentation of the instance brand for the templates (#598): the ``instance_brand`` dictionary.

Built from the immutable snapshot (``skeleton.appearance.snapshot``); URLs carry ``?v=<version>``
so browsers cache them until the brand changes, and are empty when there is nothing to show (no
``<link>`` and no ``<img>``: without a brand the HTML is the same as before). Under
``/platform/appearance/`` the stylesheet is the safe one (tokens only, ``safe=1``): a free CSS
that hides the interface never locks the administrator out of the page that fixes it.
"""

from __future__ import annotations

from typing import Any

SAFE_PREFIX = "/platform/appearance/"
PATHS = {
    "theme": "/brand/theme.css",
    "logo": "/brand/logo/",
    "logo_dark": "/brand/logo-dark/",
    "icon": "/brand/icon/",
    "manifest": "/brand/manifest.webmanifest",
}


def instance_brand(appearance: Any, *, safe: bool = False) -> dict[str, str]:
    """``{name, footer_text, color, theme_url, logo_url, logo_dark_url, icon_url, manifest_url}`` of ``appearance``."""
    version = appearance.theme_safe_version if safe else appearance.theme_version
    theme_url = ""
    if version:
        theme_url = f"{PATHS['theme']}?v={version}" + ("&safe=1" if safe else "")
    logo_url = f"{PATHS['logo']}?v={appearance.logo_version}" if appearance.logo_version else ""
    dark_url = (
        f"{PATHS['logo_dark']}?v={appearance.logo_dark_version}" if logo_url and appearance.logo_dark_version else ""
    )
    return {
        "name": appearance.product_name,
        "footer_text": appearance.footer_text,
        "color": appearance.brand_color,
        "theme_url": theme_url,
        "logo_url": logo_url,
        "logo_dark_url": dark_url,
        "icon_url": f"{PATHS['icon']}?v={appearance.icon_version}" if appearance.icon_version else "",
        # #639: the PWA manifest of the main domain, only when the instance has something to describe
        # (without a brand the HTML stays the same as before).
        "manifest_url": PATHS["manifest"]
        if appearance.product_name or appearance.icon_version or appearance.brand_color
        else "",
    }


def is_safe_path(path: str) -> bool:
    return path.startswith(SAFE_PREFIX)
