"""Pinned Python wheels: markers, launchers, ownership and the complete wheelhouse."""

import hashlib
import io
import re
import zipfile
from configparser import ConfigParser
from configparser import Error as ConfigParserError
from pathlib import Path, PurePosixPath

from packaging.markers import Marker, default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version

from skeleton.versioning import version_satisfies

from . import limits, zipio
from .errors import PackageError, _fail
from .limits import INTERPRETER_LAUNCHER, RESERVED_LAUNCHERS
from .primitives import _path, _python_version, _specifier
from .zipio import _read_member, _zip_members


def _target_environment(value=None):
    environment = default_environment()
    environment["extra"] = ""
    if value is not None:
        if not isinstance(value, dict) or any(
            key not in environment or not isinstance(item, str) for key, item in value.items()
        ):
            _fail("Invalid target marker environment.")
        environment.update(value)
        if "python_full_version" in value and "python_version" not in value:
            environment["python_version"] = ".".join(
                str(part) for part in _python_version(environment["python_full_version"]).release[:2]
            )
    full = _python_version(environment["python_full_version"])
    short = _python_version(environment["python_version"])
    if full.release[:2] != short.release[:2]:
        _fail("Target Python marker versions disagree.")
    return environment


def _marker_active(marker, environment):
    try:
        return not marker or Marker(marker).evaluate(environment=environment)
    except (ValueError, TypeError, KeyError) as exc:
        raise PackageError("Invalid target dependency marker.") from exc


def _wheel_scripts(archive, files, dist_info, distribution):
    entry_path = dist_info + "/entry_points.txt"
    if entry_path not in files:
        return set()
    parser = ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    try:
        parser.read_string(_read_member(archive, files[entry_path], limits.MAX_METADATA_BYTES).decode("utf-8"))
    except (ConfigParserError, UnicodeError) as exc:
        raise PackageError("Invalid wheel entry points.") from exc
    scripts = set()
    for section in ("console_scripts", "gui_scripts"):
        for name, entry in parser.items(section) if parser.has_section(section) else ():
            _path(name)
            folded = name.casefold()
            launcher = re.sub(r"(?:\.exe|\.pyw?|-script\.pyw?)\Z", "", folded)
            if (
                "/" in name
                or INTERPRETER_LAUNCHER.fullmatch(launcher)
                or re.fullmatch(r"(?:activate|deactivate)(?:[._-].*)?", launcher)
                or launcher == "skeleton-deployment-agent"
            ):
                _fail("Wheel entry point replaces a reserved interpreter/operator launcher.")
            if launcher in RESERVED_LAUNCHERS and distribution != RESERVED_LAUNCHERS[launcher]:
                _fail("Wheel entry point replaces a framework launcher.")
            if not re.fullmatch(
                r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*(?:\s*\[[A-Za-z0-9_, .-]+\])?", entry
            ):
                _fail("Invalid executable wheel entry point.")
            # Include POSIX and Windows wrappers, regardless of build host.
            generated = {
                f"scripts/{folded}",
                f"scripts/{folded}.exe",
                f"scripts/{folded}-script.py",
                f"scripts/{folded}-script.pyw",
            }
            if generated & scripts:
                _fail("Wheel generated executable collision.")
            scripts.update(generated)
    return scripts


def _wheel(data, dependency, *, core=False):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            files = _zip_members(archive, limits.MAX_WHEEL_MEMBERS)
            distribution = canonicalize_name(dependency["name"])
            name = distribution.replace("-", "_")
            dist_info = f"{name}-{dependency['version']}.dist-info"
            metadata_paths = [p for p in files if p.endswith(".dist-info/METADATA")]
            if len(metadata_paths) != 1 or metadata_paths[0].split("/")[0].casefold() != dist_info.casefold():
                _fail("Wheel metadata identity mismatch.")
            from email.parser import BytesParser

            metadata = BytesParser().parsebytes(
                _read_member(archive, files[metadata_paths[0]], limits.MAX_METADATA_BYTES)
            )
            if (
                canonicalize_name(metadata.get("Name", "")) != distribution
                or str(Version(metadata.get("Version", ""))) != dependency["version"]
            ):
                _fail("Wheel metadata version mismatch.")
            is_core = distribution == "django-skeleton"
            if is_core and not core:
                _fail("Application cannot own the core distribution.")
            for path, info in files.items():
                root = path.split("/")[0]
                if (
                    ".data" in root
                    or (root.endswith(".dist-info") and root != dist_info)
                    or (is_core and root not in ("skeleton", dist_info))
                    or (
                        not is_core
                        and root.split(".")[0].casefold()
                        in ("applications", "skeleton", "config", "examples", "deployment_agent")
                    )
                ):
                    _fail("Wheel escapes its permitted distribution namespace.")
                folded_root = root.casefold().split(".")[0]
                if path.casefold().endswith((".pth", ".pyc", ".pyo")) or folded_root in (
                    "sitecustomize",
                    "usercustomize",
                    "site",
                    "os",
                    "sys",
                ):
                    _fail("Wheel startup hooks and precompiled interpreter files are forbidden.")
                if folded_root == "django" and distribution != "django":
                    _fail("Only Django may own the framework namespace.")
                _read_member(archive, info)
            if is_core and "skeleton/__init__.py" not in files:
                _fail("Core wheel lacks skeleton namespace.")
            roots = {
                path.split("/")[0].casefold().split(".")[0]
                for path in files
                if not path.split("/")[0].endswith(".dist-info")
            }
            destinations = {"site-packages/" + path.casefold() for path in files}
            destinations.update(
                "site-packages/" + dist_info.casefold() + "/" + name.casefold()
                for name in ("INSTALLER", "REQUESTED", "direct_url.json")
            )
            destinations.update(_wheel_scripts(archive, files, dist_info, distribution))
            return destinations, roots, sum(info.file_size for info in files.values()), len(files), metadata
    except (zipfile.BadZipFile, RuntimeError, OSError, ValueError) as exc:
        if isinstance(exc, PackageError):
            raise
        raise PackageError("Invalid isolated wheel.") from exc


def _claim_wheel(destinations, roots, owner, ownership, namespaces):
    for destination in destinations:
        if destination in ownership and ownership[destination] != owner:
            _fail("Wheel installed destination collision.")
        for parent in PurePosixPath(destination).parents:
            previous = ownership.get(str(parent))
            if previous is not None and previous != owner:
                _fail("Wheel installed file/directory collision.")
    if any(
        previous != owner and any(path.startswith(destination + "/") for destination in destinations)
        for path, previous in ownership.items()
    ):
        _fail("Wheel installed file/directory collision.")
    for namespace in roots:
        if namespace in namespaces and namespaces[namespace] != owner:
            _fail("Wheel Python namespace collision.")
    ownership.update(dict.fromkeys(destinations, owner))
    namespaces.update(dict.fromkeys(roots, owner))


def validate_wheel_set(paths, target_environment=None):
    """Validate the complete runtime wheelhouse before pip can overwrite files.

    Return case-folded site-packages/<path> and scripts/<name> ownership entries,
    each containing name, version and sha256. Identical distribution/digest reuse
    is allowed; .pth startup hooks and bootstrap/operator launchers are not.
    Marker and Requires-Python evaluation uses the explicit target environment.
    """
    environment = _target_environment(target_environment)
    ownership, namespaces, distributions = {}, {}, {}
    expanded_total = member_total = 0
    for path in paths:
        path = Path(path)
        try:
            name, version, _, _ = parse_wheel_filename(path.name)
        except ValueError as exc:
            raise PackageError("Invalid wheel filename.") from exc
        data = zipio._protected_snapshot(path, limits.MAX_FILE_BYTES)
        owner = {"name": name, "version": str(version), "sha256": hashlib.sha256(data).hexdigest()}
        if name in distributions:
            if distributions[name][0] != owner:
                _fail("Conflicting fixed Python dependency versions or builds.")
            continue
        destinations, roots, expanded, members, metadata = _wheel(data, owner, core=True)
        expanded_total += expanded
        member_total += members
        if expanded_total > limits.MAX_TOTAL_BYTES or member_total > limits.MAX_WHEELHOUSE_MEMBERS:
            _fail("Combined wheelhouse expansion/member limit exceeded.")
        _claim_wheel(destinations, roots, owner, ownership, namespaces)
        if metadata.get("Requires-Python") and not version_satisfies(
            environment["python_full_version"], _specifier(metadata["Requires-Python"])
        ):
            _fail("Incompatible wheel Python version.")
        try:
            requirements = [Requirement(value) for value in metadata.get_all("Requires-Dist", ())]
        except ValueError as exc:
            raise PackageError("Invalid wheel dependency metadata.") from exc
        distributions[name] = (owner, requirements)
    extras = {name: {""} for name in distributions}
    changed = True
    while changed:
        changed = False
        for name, (_, requirements) in distributions.items():
            for requirement in requirements:
                if requirement.marker and not any(
                    requirement.marker.evaluate(environment={**environment, "extra": extra})
                    for extra in tuple(extras[name])
                ):
                    continue
                dependency = canonicalize_name(requirement.name)
                if (
                    requirement.url
                    or dependency not in distributions
                    or not requirement.specifier.contains(distributions[dependency][0]["version"], prereleases=True)
                ):
                    _fail("Missing or incompatible wheel dependency.")
                requested = set(requirement.extras) - extras[dependency]
                if requested:
                    extras[dependency].update(requested)
                    changed = True
    return ownership
