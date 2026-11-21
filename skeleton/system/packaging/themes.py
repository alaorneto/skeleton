"""The instance themes an app ships (#642), checked by package build and package doctor.

Each entry of ``runtime.instance_themes`` names a directory of the app with the theme package
format of epic #629. The reader (``skeleton.appearance.package_format``) imports nothing from
Django, so this runs where ``package build`` runs, without settings. The AA contrast of the colour
needs Django: the check E047 and the registry of app themes (``skeleton.appearance.app_themes``)
do it at load.
"""

from pathlib import Path

from packaging.specifiers import SpecifierSet
from packaging.version import Version


def theme_id(app_id, slug):
    """The id the theme.json of an app theme must have: ``<app_id>.theme.<slug>``."""
    return f"{app_id}.theme.{slug}"


def _app_floor(specifier):
    """The lowest core the app accepts (``None`` without a lower bound)."""
    floors = [
        Version(item.version.removesuffix(".*"))
        for item in SpecifierSet(specifier)
        if item.operator in (">=", "==", "~=", ">")
    ]
    return max(floors) if floors else None


def instance_theme_problems(root, manifest):
    """``[(slug, code, message, path, line)]`` of every declared theme; ``[]`` when all are valid.

    ``code`` is a code of ``package_format.ERROR_CODES``, or ``theme_id`` (the theme.json id is not
    ``<app_id>.theme.<slug>``) or ``theme_compatibility`` (the lowest core of the app is outside
    the range of the theme).
    """
    from skeleton.appearance.package_format import ThemePackageError, read_theme_directory

    problems = []
    root = Path(root).resolve()
    for entry in manifest["runtime"].get("instance_themes", ()):
        slug, directory = entry["slug"], (root / entry["path"]).resolve()
        if not directory.is_relative_to(root):
            problems.append((slug, "archive", "The theme directory escapes the application root.", entry["path"], 0))
            continue
        try:
            package = read_theme_directory(directory)
        except ThemePackageError as error:
            problems.append((slug, error.code, str(error), error.path, error.line))
            continue
        expected = theme_id(manifest["app_id"], slug)
        if package.id != expected:
            problems.append((slug, "theme_id", f"theme.json id must be {expected}.", "theme.json", 0))
        floor = _app_floor(manifest["compatibility"]["skeleton"])
        if floor is not None and not SpecifierSet(package.compatibility).contains(floor, prereleases=True):
            problems.append(
                (
                    slug,
                    "theme_compatibility",
                    f"The lowest core of the app ({floor}) is outside the theme range {package.compatibility}.",
                    "theme.json",
                    0,
                )
            )
    return problems
