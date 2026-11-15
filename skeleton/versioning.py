"""Release identities and compatibility, usable without Django configuration."""

import re
import tomllib
from importlib import metadata
from pathlib import Path

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version


def normalize_version(value):
    """Accept SemVer (including -rc.N) and return its PEP 440 equivalent."""
    if not isinstance(value, str) or not value:
        raise ValueError("Version must be a nonempty string.")
    converted = re.sub(
        r"-(alpha|beta|rc)\.?([0-9]+)", lambda m: {"alpha": "a", "beta": "b", "rc": "rc"}[m[1]] + m[2], value
    )
    try:
        parsed = Version(converted)
    except InvalidVersion as exc:
        raise ValueError("Invalid component version.") from exc
    if len(parsed.release) != 3 or parsed.epoch:
        raise ValueError("Component versions require major.minor.patch.")
    return str(parsed)


def validate_specifier(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Compatibility requires an explicit version range.")
    try:
        return str(SpecifierSet(value))
    except InvalidSpecifier as exc:
        raise ValueError("Invalid compatibility range.") from exc


def version_satisfies(version, specifier):
    """Whether version is inside the range; pre-releases count, with any supported packaging (#256)."""
    return bool(SpecifierSet(validate_specifier(specifier)).contains(Version(str(version)), prereleases=True))


def release_line(version):
    """``(major, minor)`` of a component version: the line it belongs to (#493)."""
    parsed = Version(normalize_version(version))
    return parsed.major, parsed.minor


def adjacent_release_line(current, target):
    """True when ``target`` is in the line of ``current``, in the next minor of the same major,
    or in the first line (``X+1.0``) of the next major: an update never skips a line (#493)."""
    (current_major, current_minor), (target_major, target_minor) = release_line(current), release_line(target)
    if target_major == current_major:
        return 0 <= target_minor - current_minor <= 1
    return target_major == current_major + 1 and target_minor == 0


def upgrade_sources(version, known_versions=(), *, earliest=None):
    """Installed versions a release accepts: its own line and the line right before it (#494).

    ``known_versions`` are the versions already published; ``earliest`` is used only when no
    earlier line is known (the first automated release). Returns ``>=<floor>,<=<version>``.
    """
    version = normalize_version(version)
    line = release_line(version)
    earlier = {
        release_line(value)
        for value in (normalize_version(known) for known in known_versions)
        if Version(value) < Version(version)
    }
    previous = max((item for item in earlier if item < line), default=None)
    if previous is not None:
        floor = f"{previous[0]}.{previous[1]}.0"
    elif earliest is not None:
        floor = normalize_version(earliest)
    else:
        floor = version
    return f">={floor},<={version}"


def core_version():
    """Installed distribution metadata is authoritative; checkout is explicitly source."""
    try:
        distribution = metadata.distribution("django-skeleton")
    except metadata.PackageNotFoundError:
        distribution = None
    module = Path(__file__).resolve()
    if distribution is not None:
        installed = Path(distribution.locate_file("skeleton/versioning.py")).resolve()
        if installed == module:
            return {"version": normalize_version(distribution.version), "origin": "installed"}
    project = module.parent.parent / "pyproject.toml"
    if project.is_file():
        data = tomllib.loads(project.read_text(encoding="utf-8"))
        if data.get("project", {}).get("name") == "django-skeleton":
            return {"version": normalize_version(data["project"]["version"]), "origin": "source"}
    return {"version": None, "origin": "unknown"}
