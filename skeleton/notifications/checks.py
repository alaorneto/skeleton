from django.core.checks import Error, register


@register()
def notification_type_checks(app_configs=None, **kwargs):
    from .registry import notification_problems

    return [Error(problem, id="skeleton.E023") for problem in notification_problems()]
