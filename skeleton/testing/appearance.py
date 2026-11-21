"""Test helpers for apps that ship instance themes (#645): only for test suites.

``assert_app_themes_valid(app_label)`` fails, listing every problem, when a theme the app declares
in ``runtime.instance_themes`` would be refused by the core: the package format, the AA contrast,
the core range or the ``<app_id>.theme.<slug>`` id rule. It runs the same checks as E047.
"""

from __future__ import annotations

__all__ = ["assert_app_themes_valid"]


def assert_app_themes_valid(app_label: str) -> None:
    """Raise ``AssertionError`` with every problem of the themes of ``app_label``."""
    from skeleton.appearance.app_themes import declared_theme_problems
    from skeleton.system.app_registry import all_apps

    manifest = next((item for item in all_apps() if item.app_label == app_label), None)
    if manifest is None:
        raise AssertionError(f"The app {app_label} is not registered.")
    if not manifest.instance_themes:
        raise AssertionError(f"The app {app_label} declares no instance theme (runtime.instance_themes).")
    problems = declared_theme_problems(manifest)
    if problems:
        details = "; ".join(f"{slug}: {message}" for slug, message in problems)
        raise AssertionError(f"Invalid instance themes of {app_label}: {details}")
