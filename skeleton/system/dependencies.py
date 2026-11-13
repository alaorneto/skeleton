"""Structured dependency resolution for the core and installable applications.

This module never imports Django or application code, so the deployment agent, the
packaging CLI and the runtime share one resolver. Problems carry only identities,
version ranges and versions: never paths, payloads or secrets.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from importlib.metadata import version as distribution_version
from typing import Any

from packaging.utils import canonicalize_name
from packaging.version import Version

from skeleton.versioning import core_version, version_satisfies

CORE_COMPONENT_ID = "django-skeleton"

# Stable public codes; each keeps the message validate_dependency_graph always raised.
MESSAGES = {
    "wheel_pin_conflict": "Conflicting fixed Python dependency versions or builds.",
    "identity_collision": "Component identity collision.",
    "namespace_collision": "Application identity/namespace collision.",
    "incompatible_platform": "Incompatible {dependency} version for {component}.",
    "missing_dependency": "Missing or incompatible application dependency.",
    "incompatible_dependency_version": "Missing or incompatible application dependency.",
    "dependency_cycle": "Cyclic component dependency.",
    "conflicting_release": "Conflicting application releases.",
}


@dataclass(frozen=True)
class DependencyProblem:
    code: str
    component: str
    dependency: str | None = None
    required: str | None = None
    found: str | None = None
    path: tuple[str, ...] = ()

    @property
    def message(self) -> str:
        return MESSAGES[self.code].format(component=self.component, dependency=self.dependency)

    @property
    def summary(self) -> str:
        """One line for system checks and CLI output: code, components, ranges and versions."""
        text = f"{self.code}: {self.component}"
        if self.dependency:
            text += f" -> {self.dependency}"
        if self.required:
            text += f" {self.required}"
        if self.found:
            text += f" (found {self.found})"
        if self.path:
            text += " [" + " -> ".join(self.path) + "]"
        return text

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"code": self.code, "component": self.component}
        for name in ("dependency", "required", "found"):
            value = getattr(self, name)
            if value is not None:
                data[name] = value
        if self.path:
            data["path"] = list(self.path)
        return data


@dataclass(frozen=True)
class Resolution:
    order: tuple[str, ...]
    problems: tuple[DependencyProblem, ...] = field(default=())

    @property
    def ok(self) -> bool:
        return not self.problems

    def raise_for_problems(self) -> None:
        from skeleton.system.packages import PackageError

        if self.problems:
            raise PackageError(self.problems[0].message)


def component_id(manifest: Mapping[str, Any]) -> str:
    return str(manifest.get("app_id", manifest.get("component_id")))


def resolve_dependencies(
    manifests: Iterable[Mapping[str, Any]],
    *,
    core: str | None = None,
    python: str | None = None,
    django: str | None = None,
    target_environment: Mapping[str, str] | None = None,
    provided: Iterable[str] = (),
) -> Resolution:
    """Validate the core and all apps together, reporting every dependency problem.

    Malformed manifests and an inconsistent target description still raise
    PackageError immediately: they are input errors, not dependency problems.
    The order lists applications dependency-first, deterministically.
    provided lists the IDs of the first-party apps the target core installs (#254): they
    satisfy a dependency at the core's own version and never enter the order.
    """
    from skeleton.system.packages import PackageError, _marker_active, _target_environment, validate_manifest

    inventory = [validate_manifest(dict(value)) for value in manifests]
    selected_core = [m for m in inventory if m["kind"] == "core"]
    if len(selected_core) > 1:
        raise PackageError("Core component identity collision.")
    core = core or (selected_core[0]["version"] if selected_core else core_version()["version"])
    provided_ids = frozenset(provided)
    environment = _target_environment(dict(target_environment) if target_environment is not None else None)
    python = python or environment["python_full_version"]
    if (
        target_environment
        and "python_full_version" in target_environment
        and Version(python) != Version(environment["python_full_version"])
    ):
        raise PackageError("Target Python version and marker environment disagree.")
    environment["python_full_version"] = python
    environment["python_version"] = ".".join(str(part) for part in Version(python).release[:2])
    django = django or distribution_version("Django")
    if selected_core and Version(selected_core[0]["version"]) != Version(core):
        raise PackageError("Core manifest version differs from the selected core.")

    problems: list[DependencyProblem] = []
    components: dict[str, Mapping[str, Any]] = {}
    labels: set[str] = set()
    packages: set[str] = set()
    pins: dict[str, tuple[str, str, str]] = {}
    for m in inventory:
        identity = component_id(m)
        if identity in provided_ids:
            # A third-party app never takes the identity of a first-party app (#254).
            problems.append(DependencyProblem("identity_collision", identity))
            continue
        for dependency in m["python_dependencies"]:
            if not _marker_active(dependency.get("marker"), environment):
                continue
            name = canonicalize_name(dependency["name"])
            pin = (dependency["version"], dependency["sha256"])
            if name in pins and pins[name][:2] != pin:
                problems.append(
                    DependencyProblem(
                        "wheel_pin_conflict",
                        identity,
                        name,
                        required=pin[0],
                        found=pins[name][0],
                        path=(pins[name][2],),
                    )
                )
            else:
                pins[name] = (*pin, identity)
        if identity in components:
            problems.append(DependencyProblem("identity_collision", identity))
            continue
        if m["kind"] == "app":
            if m["app_label"] in labels or m["package_name"] in packages:
                problems.append(DependencyProblem("namespace_collision", identity))
                continue
            labels.add(m["app_label"])
            packages.add(m["package_name"])
        components[identity] = m
        for target, running in {"skeleton": core, "python": python, "django": django}.items():
            spec = m["compatibility"][target]
            if running is None or not version_satisfies(running, spec):
                problems.append(DependencyProblem("incompatible_platform", identity, target, spec, running))

    order: list[str] = []
    visited: set[str] = set()
    stack: list[str] = []
    reported_cycles: set[frozenset[str]] = set()

    def visit(identity: str) -> None:
        if identity in stack:
            cycle = (*stack[stack.index(identity) :], identity)
            key = frozenset(cycle)
            if key not in reported_cycles:
                reported_cycles.add(key)
                problems.append(DependencyProblem("dependency_cycle", identity, path=cycle))
            return
        if identity in visited:
            return
        stack.append(identity)
        manifest = components[identity]
        for dependency, spec in sorted(manifest["dependencies"].items()):
            target = components.get(dependency)
            if target is None:
                if dependency in provided_ids:
                    # First-party apps ship in the core wheel at the core's own version (#66, #254).
                    if core is None or not version_satisfies(core, spec):
                        problems.append(
                            DependencyProblem("incompatible_dependency_version", identity, dependency, spec, core)
                        )
                    continue
                problems.append(DependencyProblem("missing_dependency", identity, dependency, spec))
                continue
            if not version_satisfies(target["version"], spec):
                problems.append(
                    DependencyProblem("incompatible_dependency_version", identity, dependency, spec, target["version"])
                )
            visit(dependency)
        for conflict, spec in sorted(manifest.get("conflicts", {}).items()):
            other = components.get(conflict)
            if other is not None and version_satisfies(other["version"], spec):
                problems.append(DependencyProblem("conflicting_release", identity, conflict, spec, other["version"]))
            elif other is None and conflict in provided_ids and core is not None and version_satisfies(core, spec):
                problems.append(DependencyProblem("conflicting_release", identity, conflict, spec, core))
        stack.pop()
        visited.add(identity)
        if manifest["kind"] == "app":
            order.append(identity)

    for identity in sorted(components):
        visit(identity)
    return Resolution(tuple(order), tuple(problems))


def dependents_of(manifests: Iterable[Mapping[str, Any]], app_ids: Iterable[str]) -> list[str]:
    """Return every component that transitively depends on app_ids, excluding them.

    Expects already validated manifests (inventory entries or verified artifacts).
    """
    targets = set(app_ids)
    reverse: dict[str, set[str]] = {}
    for manifest in manifests:
        identity = component_id(manifest)
        for dependency in manifest.get("dependencies", {}):
            reverse.setdefault(dependency, set()).add(identity)
    found: set[str] = set()
    pending = list(targets)
    while pending:
        for dependent in reverse.get(pending.pop(), ()):
            if dependent not in found and dependent not in targets:
                found.add(dependent)
                pending.append(dependent)
    return sorted(found)


def dependency_closure(manifests: Iterable[Mapping[str, Any]], app_id: str) -> list[str]:
    """Return every component app_id transitively requires, dependency-first, excluding it."""
    graph = {component_id(m): tuple(m.get("dependencies", {})) for m in manifests}
    order: list[str] = []
    seen: set[str] = {app_id}

    def visit(identity: str) -> None:
        for dependency in graph.get(identity, ()):
            if dependency not in seen:
                seen.add(dependency)
                visit(dependency)
                order.append(dependency)

    visit(app_id)
    return order


def catalog_candidates(
    entries: Iterable[Mapping[str, Any]],
    dependency: str,
    required: str,
    target: Mapping[str, str],
    *,
    limit: int = 3,
) -> list[Mapping[str, Any]]:
    """Catalogue entries for dependency inside required and compatible with target, newest first.

    target maps skeleton/python/django to the versions the release would run with.
    """
    matches = [
        entry
        for entry in entries
        if entry["component_id"] == dependency
        and version_satisfies(entry["version"], required)  # Pre-releases count (#256).
        and all(version_satisfies(version, entry["compatibility"][key]) for key, version in target.items())
    ]
    matches.sort(key=lambda entry: Version(entry["version"]), reverse=True)
    return matches[:limit]
