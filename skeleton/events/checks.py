from django.core.checks import Error, register


@register()
def event_type_checks(app_configs=None, **kwargs):
    from .registry import event_problems

    return [Error(problem, id="skeleton.E022") for problem in event_problems()]
