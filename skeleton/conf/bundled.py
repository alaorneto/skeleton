"""Fixed map of the first-party apps an operator may opt into (#66).

``SKELETON_BUNDLED_APPS`` lists names from this map; the environment never supplies an import
path. An unknown name stops the startup with ``ImproperlyConfigured``. The core imports nothing
from ``skeleton.contrib``: these are strings handed to ``INSTALLED_APPS``.
"""

from __future__ import annotations

from django.core.exceptions import ImproperlyConfigured

# name -> AppConfig path. Empty until the first first-party app ships (the calendar, #84).
BUNDLED_APPS: dict[str, str] = {}


def bundled_app_configs(names, *, available: dict[str, str] | None = None) -> list[str]:
    available = BUNDLED_APPS if available is None else available
    configs = []
    for name in names:
        name = name.strip()
        if not name:
            continue
        if name not in available:
            raise ImproperlyConfigured(f"Unknown first-party app in SKELETON_BUNDLED_APPS: {name!r}.")
        if available[name] not in configs:
            configs.append(available[name])
    return configs
