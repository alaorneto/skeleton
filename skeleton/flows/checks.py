from django.core.checks import Error, register


@register()
def flow_checks(app_configs=None, **kwargs):
    from .registry import flow_problems

    return [Error(problem, id="skeleton.E029") for problem in flow_problems()]
