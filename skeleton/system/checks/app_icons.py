"""E051 (#842): the icon of an app menu entry cannot be drawn.

The concept does not exist in the core, the SVG is missing or is not an accepted 24×24 stroke
icon, or it is drawn like an icon of the core or of another app (one concept, one icon). The app
loads normally and the entry keeps the ``apps`` icon (``skeleton.system.app_icons``). The message
names the app, the entry and the problem, never a path of the server.
"""

from django.core.checks import Error, register


@register()
def app_icon_checks(app_configs=None, **kwargs):
    from ..app_icons import icon_problems

    return [
        Error(
            f"A entrada {entry} do menu da app {app_label} tem um ícone inválido: {problem}.",
            hint="Use em icon um conceito do núcleo ou, em icon_svg, um ícone de traço próprio; veja docs/extending.md.",
            obj=app_label,
            id="skeleton.E051",
        )
        for app_label, entry, problem in icon_problems()
    ]
