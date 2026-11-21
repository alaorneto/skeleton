"""Check E045 (#620): the dashboard widgets registered by apps."""

from django.core.checks import Error, register


@register()
def dashboard_widget_checks(app_configs=None, **kwargs):
    from skeleton.system.dashboard_widgets import dashboard_widget_problems

    return [
        Error(
            problem,
            hint="Corrija o register_dashboard_widget da app (docs/extending.md, quadros do painel).",
            id="skeleton.E045",
        )
        for problem in dashboard_widget_problems()
    ]
