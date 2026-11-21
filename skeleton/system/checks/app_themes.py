"""E047 (#642): an instance theme an app ships is invalid.

The theme is read at check time (format, AA contrast, core range and the
``<app_id>.theme.<slug>`` id rule) by ``skeleton.appearance.app_themes``; the app itself loads
normally and an invalid theme simply stays out of the catalog. The message names the app, the
theme and the problem, never a path of the server.
"""

from django.core.checks import Error, register


@register()
def app_theme_checks(app_configs=None, **kwargs):
    from skeleton.appearance.app_themes import declared_theme_problems

    from ..app_registry import all_apps

    errors = []
    for manifest in all_apps():
        for slug, message in declared_theme_problems(manifest):
            errors.append(
                Error(
                    f"A app {manifest.app_label} declara o tema {slug} inválido: {message}",
                    hint="Rode manage.py appearance check-package no diretório do tema e corrija; veja docs/themes.md.",
                    obj=manifest.app_label,
                    id="skeleton.E047",
                )
            )
    return errors
