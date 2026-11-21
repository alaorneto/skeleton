"""Instance themes shipped by installed apps (epic #630).

An app declares theme directories in ``runtime.instance_themes`` of its manifest (#642). A theme of
an app is never copied to the database: it exists exactly while the code of the app is installed,
as the built-in themes exist while the code of the core is. Uninstalling the app (the deployment
agent swaps the code and restarts the processes) takes the theme out of the catalog with no
synchronization step.

``check_declared_theme`` reads one directory with the package reader and applies the rules the
installation of a zip applies (AA contrast, core range) plus the id rule of app themes
(``<app_id>.theme.<slug>``). It answers a package or a Portuguese message, never raises.
"""

from __future__ import annotations

from typing import Any

__all__ = ["check_declared_theme", "declared_theme_problems", "expected_theme_id"]


def expected_theme_id(manifest: Any, slug: str) -> str:
    from skeleton.system.packaging.themes import theme_id

    return theme_id(manifest.app_id or manifest.app_label, slug)


def check_declared_theme(manifest: Any, declaration: Any) -> tuple[Any, str]:
    """``(package, "")`` for a valid theme, ``(None, message)`` otherwise."""
    from django.core.exceptions import ValidationError
    from django.utils.translation import gettext

    from skeleton.tenancy.branding import validate_brand_color
    from skeleton.versioning import core_version

    from .package_format import ThemePackageError, check_core_compatibility, read_theme_directory
    from .package_messages import describe, package_error_message

    try:
        package = read_theme_directory(declaration.path)
        check_core_compatibility(package, core_version()["version"])
    except ThemePackageError as error:
        return None, describe(error)
    expected = expected_theme_id(manifest, declaration.slug)
    if package.id != expected:
        return None, gettext("o id do theme.json precisa ser %(id)s.") % {"id": expected}
    if package.brand_color:
        try:
            validate_brand_color(package.brand_color)
        except ValidationError:
            return None, package_error_message("brand_color")
    return package, ""


def declared_theme_problems(manifest: Any) -> list[tuple[str, str]]:
    """``[(slug, message)]`` of the invalid themes an app declares (check E047)."""
    problems = []
    for declaration in manifest.instance_themes:
        package, message = check_declared_theme(manifest, declaration)
        if package is None:
            problems.append((declaration.slug, message))
    return problems
