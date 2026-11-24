"""Fixed map of the first-party apps an operator may opt into (#66).

``SKELETON_BUNDLED_APPS`` lists names from this map; the environment never supplies an import
path. An unknown name stops the startup with ``ImproperlyConfigured``. The core imports nothing
from ``skeleton.contrib``: these are strings handed to ``INSTALLED_APPS``.

A first-party **library** (#792, #793) is also installed without the opt-in when an installed app
depends on it, with the libraries it depends on: ``required_libraries`` reads only the
``manifest.json`` files shipped in the wheel (JSON, never code).
"""

from __future__ import annotations

from django.core.exceptions import ImproperlyConfigured

# name -> AppConfig path.
BUNDLED_APPS: dict[str, str] = {
    "calendar": "skeleton.contrib.calendar.apps.CalendarConfig",
    "ledger": "skeleton.contrib.ledger.apps.LedgerConfig",
}
# name -> app label, and the frozen schema snapshot shipped in the wheel (relative to skeleton/),
# used when the app is opted out after migrating (SKELETON_BUNDLED_RETAINED, #67).
BUNDLED_LABELS: dict[str, str] = {"calendar": "skeleton_calendar", "ledger": "skeleton_ledger"}
BUNDLED_SNAPSHOTS: dict[str, str] = {
    "calendar": "contrib/calendar/retained_schema.json",
    "ledger": "contrib/ledger/retained_schema.json",
}
# name -> stable app_id of the manifest shipped in skeleton/contrib/<package>/manifest.json (#254).
BUNDLED_APP_IDS: dict[str, str] = {"calendar": "org.skeleton.calendar", "ledger": "org.skeleton.ledger"}
# name -> manifest shipped in the wheel (relative to skeleton/).
BUNDLED_MANIFESTS: dict[str, str] = {
    "calendar": "contrib/calendar/manifest.json",
    "ledger": "contrib/ledger/manifest.json",
}
LIBRARY_KIND = "bundled_library"  # skeleton.system.packaging.limits.BUNDLED_LIBRARY_KIND, without the import.


def _shipped_manifest(name: str) -> dict:
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / BUNDLED_MANIFESTS[name]
    return json.loads(path.read_text(encoding="utf-8"))


def bundled_libraries() -> tuple[str, ...]:
    """Names of the map whose shipped manifest is a first-party library, in map order."""
    return tuple(name for name in BUNDLED_APPS if _shipped_manifest(name).get("kind") == LIBRARY_KIND)


def library_app_ids() -> tuple[str, ...]:
    """Stable IDs of the libraries: always provided by the core wheel to a dependent app (#793)."""
    return tuple(BUNDLED_APP_IDS[name] for name in bundled_libraries())


def required_libraries(dependency_ids) -> tuple[str, ...]:
    """Names of the libraries ``dependency_ids`` require, with the libraries those require, in map order.

    Non-library IDs are ignored: a first-party app (``bundled_app``) keeps its explicit opt-in.
    """
    libraries = set(bundled_libraries())
    by_id = {BUNDLED_APP_IDS[name]: name for name in libraries}
    found: set[str] = set()
    pending = [by_id[identity] for identity in dependency_ids if identity in by_id]
    while pending:
        name = pending.pop()
        if name in found:
            continue
        found.add(name)
        pending.extend(
            by_id[identity] for identity in _shipped_manifest(name).get("dependencies", {}) if identity in by_id
        )
    return tuple(name for name in BUNDLED_APPS if name in found)


def libraries_required_by(manifests) -> tuple[str, ...]:
    """The libraries required by validated app manifests (mappings with ``dependencies``)."""
    return required_libraries(identity for manifest in manifests for identity in manifest.get("dependencies", {}))


ACTIVE_INVENTORY_STATES = frozenset({"installed", "project-local"})


def _inventory_requirements(entries) -> set[str]:
    return set(
        libraries_required_by(entry["manifest"] for entry in entries if entry.get("state") in ACTIVE_INVENTORY_STATES)
    )


def next_retained_libraries(previous, entries, opted_in) -> list[str]:
    """The ``retained_libraries`` of the next inventory the agent writes (#793).

    A library installed for an app (required by the previous inventory, or already retained)
    that no active app of ``entries`` requires now, and that the operator did not opt in, stays
    retained: its tables are classified by the snapshot in the wheel and its data are kept. It is
    installed again as soon as an app that depends on it comes back.
    """
    before = _inventory_requirements(previous.get("apps", [])) | set(previous.get("retained_libraries", []))
    return sorted(before - _inventory_requirements(entries) - set(opted_in))


def installed_bundled_names(opted_in, *required) -> tuple[str, ...]:
    """The opted-in names and every required library, once each, opt-in first (startup order)."""
    result: list[str] = []
    for name in (name.strip() for group in (opted_in, *required) for name in group):
        if name and name not in result:
            result.append(name)
    return tuple(result)


def project_library_configs(app_configs, *, installed=()) -> list[str]:
    """AppConfig paths of the libraries that project apps (``PROJECT_APPS``) require and are not installed.

    Reads ``manifest.json`` next to each app package, found with ``importlib.util.find_spec``
    (which imports only the parent packages, never the AppConfig). An app without a manifest
    requires nothing here.
    """
    import importlib.util
    import json
    from pathlib import Path

    manifests = []
    for path in app_configs:
        package = path.rsplit(".apps.", 1)[0] if ".apps." in path else path
        spec = importlib.util.find_spec(package)
        locations = list(spec.submodule_search_locations or ()) if spec is not None else []
        for location in locations:
            manifest = Path(location) / "manifest.json"
            if manifest.is_file():
                manifests.append(json.loads(manifest.read_text(encoding="utf-8")))
                break
    return [config for config in bundled_app_configs(libraries_required_by(manifests)) if config not in set(installed)]


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


def bundled_app_ids(names, *, identities: dict[str, str] | None = None) -> tuple[str, ...]:
    """Stable app IDs of the first-party apps opted in by name; an unknown name stops the startup.

    A managed app may depend on these IDs (#254); they are satisfied at the core's own version.
    """
    identities = BUNDLED_APP_IDS if identities is None else identities
    result: list[str] = []
    for name in (name.strip() for name in names):
        if not name:
            continue
        if name not in identities:
            raise ImproperlyConfigured(f"Unknown first-party app in SKELETON_BUNDLED_APPS: {name!r}.")
        if identities[name] not in result:
            result.append(identities[name])
    return tuple(result)


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
