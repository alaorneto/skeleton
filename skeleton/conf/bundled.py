"""Fixed map of the first-party apps an operator may opt into (#66).

``SKELETON_BUNDLED_APPS`` lists names from this map; the environment never supplies an import
path. An unknown name stops the startup with ``ImproperlyConfigured``. The core imports nothing
from ``skeleton.contrib``: these are strings handed to ``INSTALLED_APPS``.
"""

from __future__ import annotations

from django.core.exceptions import ImproperlyConfigured

# name -> AppConfig path.
BUNDLED_APPS: dict[str, str] = {"calendar": "skeleton.contrib.calendar.apps.CalendarConfig"}
# name -> app label, and the frozen schema snapshot shipped in the wheel (relative to skeleton/),
# used when the app is opted out after migrating (SKELETON_BUNDLED_RETAINED, #67).
BUNDLED_LABELS: dict[str, str] = {"calendar": "skeleton_calendar"}
BUNDLED_SNAPSHOTS: dict[str, str] = {"calendar": "contrib/calendar/retained_schema.json"}


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


def retained_snapshot_paths(names, *, installed=(), snapshots: dict[str, str] | None = None) -> list[str]:
    """Absolute snapshot paths of the first-party apps declared retained (opted out after migrating)."""
    from pathlib import Path

    snapshots = BUNDLED_SNAPSHOTS if snapshots is None else snapshots
    root = Path(__file__).resolve().parents[1]
    paths = []
    for name in (name.strip() for name in names):
        if not name:
            continue
        if name not in snapshots:
            raise ImproperlyConfigured(f"Unknown first-party app in SKELETON_BUNDLED_RETAINED: {name!r}.")
        if name in installed:
            raise ImproperlyConfigured(f"{name!r} cannot be installed and retained at the same time.")
        paths.append(str(root / snapshots[name]))
    return paths
