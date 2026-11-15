"""Update paths (#493): the ordered versions from the installed one to the newest reachable.

Pure: no network and no disk. Always taking the highest eligible version is what lets a patch be
skipped while a minor line is not: from 0.5.2, with 0.5.3, 0.6.0 and 0.6.4 accepting >=0.5.0, the
step is 0.6.4.
"""

from collections.abc import Sequence

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from skeleton.versioning import adjacent_release_line

from .types import JSON

MAX_STEPS = 32


def _eligible(entry: JSON, current: Version, *, enforce_lines: bool) -> bool:
    try:
        version = Version(entry["version"])
        accepted = SpecifierSet(entry["from_versions"]).contains(str(current), prereleases=True)
    except (InvalidSpecifier, InvalidVersion, KeyError, TypeError):
        return False
    return (
        version > current
        and bool(entry.get("compatible"))
        and accepted
        and (not enforce_lines or adjacent_release_line(str(current), entry["version"]))
    )


def component_path(
    component_id: str, kind: str, installed: str, entries: Sequence[JSON], *, enforce_lines: bool
) -> JSON:
    """``{component_id, kind, installed, latest, steps, blocked}`` of one component.

    ``entries`` are ``{version, source_id, from_versions, compatible}`` without repeated versions.
    ``blocked`` is ``incompatible_runtime`` when no newer entry fits the runtime, or
    ``missing_intermediate_release`` when the newest cannot be reached one line at a time.
    """
    current = Version(installed)
    newer = []
    for entry in entries:
        try:
            if Version(entry["version"]) > current:
                newer.append(entry)
        except (InvalidVersion, KeyError, TypeError):
            continue
    steps: list[JSON] = []
    while len(steps) < MAX_STEPS:
        eligible = [entry for entry in newer if _eligible(entry, current, enforce_lines=enforce_lines)]
        if not eligible:
            break
        chosen = max(eligible, key=lambda entry: Version(entry["version"]))
        steps.append({"version": chosen["version"], "source_id": chosen["source_id"]})
        current = Version(chosen["version"])
    latest = max(newer, key=lambda entry: Version(entry["version"]))["version"] if newer else None
    blocked = None
    if latest is not None and current < Version(latest):
        remaining = [entry for entry in newer if Version(entry["version"]) > current]
        blocked = (
            "incompatible_runtime"
            if not any(entry.get("compatible") for entry in remaining)
            else "missing_intermediate_release"
        )
    return {
        "component_id": component_id,
        "kind": kind,
        "installed": installed,
        "latest": latest,
        "steps": steps,
        "blocked": blocked,
    }
