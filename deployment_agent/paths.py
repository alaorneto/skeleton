"""Update paths (#493): the ordered versions from the installed one to the newest reachable, and
to a version the operator chose (#653).

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


def _newer(installed: Version, entries: Sequence[JSON]) -> list[JSON]:
    newer = []
    for entry in entries:
        try:
            if Version(entry["version"]) > installed:
                newer.append(entry)
        except (InvalidVersion, KeyError, TypeError):
            continue
    return newer


def _walk(
    installed: Version, newer: Sequence[JSON], *, enforce_lines: bool, ceiling: Version | None = None
) -> list[JSON]:
    """The steps from ``installed``, always the highest eligible version not above ``ceiling``."""
    current = installed
    steps: list[JSON] = []
    while len(steps) < MAX_STEPS:
        eligible = [
            entry
            for entry in newer
            if _eligible(entry, current, enforce_lines=enforce_lines)
            and (ceiling is None or Version(entry["version"]) <= ceiling)
        ]
        if not eligible:
            break
        chosen = max(eligible, key=lambda entry: Version(entry["version"]))
        steps.append({"version": chosen["version"], "source_id": chosen["source_id"]})
        current = Version(chosen["version"])
    return steps


def steps_to(installed: str, entries: Sequence[JSON], target: str, *, enforce_lines: bool) -> list[JSON] | None:
    """The steps from ``installed`` to exactly ``target`` (#653), or ``None`` when it is not reachable.

    The walk is the same as the path to the newest, capped at the target: a patch is skipped, a
    minor line never is. A target that is not newer, not published or not compatible is ``None``.
    """
    try:
        current, wanted = Version(installed), Version(target)
    except (InvalidVersion, TypeError):
        return None
    steps = _walk(current, _newer(current, entries), enforce_lines=enforce_lines, ceiling=wanted)
    return steps if steps and Version(steps[-1]["version"]) == wanted else None


def reachable_targets(installed: str, entries: Sequence[JSON], *, enforce_lines: bool) -> list[JSON]:
    """The versions an operator may choose as target (#653): ``[{version, steps}]``, oldest first.

    One per release line, its newest compatible version, when the walk reaches it: an older patch
    of a line is never a target, because the newest of the same line replaces it.
    """
    current = Version(installed)
    newest: dict[tuple[int, int], Version] = {}
    for entry in _newer(current, entries):
        if not entry.get("compatible"):
            continue
        version = Version(entry["version"])
        line = (version.major, version.minor)
        if line not in newest or version > newest[line]:
            newest[line] = version
    targets = []
    for line in sorted(newest):
        steps = steps_to(installed, entries, str(newest[line]), enforce_lines=enforce_lines)
        if steps is not None:
            targets.append({"version": steps[-1]["version"], "steps": steps})
    return targets


def component_path(
    component_id: str,
    kind: str,
    installed: str,
    entries: Sequence[JSON],
    *,
    enforce_lines: bool,
    with_targets: bool = False,
) -> JSON:
    """``{component_id, kind, installed, latest, steps, blocked}`` of one component.

    ``entries`` are ``{version, source_id, from_versions, compatible}`` without repeated versions.
    ``blocked`` is ``incompatible_runtime`` when no newer entry fits the runtime, or
    ``missing_intermediate_release`` when the newest cannot be reached one line at a time.
    ``with_targets`` adds ``targets``, the versions an operator may choose (#653, core only).
    """
    newer = _newer(Version(installed), entries)
    steps = _walk(Version(installed), newer, enforce_lines=enforce_lines)
    current = Version(steps[-1]["version"]) if steps else Version(installed)
    latest = max(newer, key=lambda entry: Version(entry["version"]))["version"] if newer else None
    blocked = None
    if latest is not None and current < Version(latest):
        remaining = [entry for entry in newer if Version(entry["version"]) > current]
        blocked = (
            "incompatible_runtime"
            if not any(entry.get("compatible") for entry in remaining)
            else "missing_intermediate_release"
        )
    path: JSON = {
        "component_id": component_id,
        "kind": kind,
        "installed": installed,
        "latest": latest,
        "steps": steps,
        "blocked": blocked,
    }
    if with_targets:
        path["targets"] = reachable_targets(installed, entries, enforce_lines=enforce_lines)
    return path
