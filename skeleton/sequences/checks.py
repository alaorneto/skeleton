from django.core.checks import Error, register


@register()
def sequence_checks(app_configs=None, **kwargs):
    from .registry import sequence_problems

    return [Error(problem, id="skeleton.E028") for problem in sequence_problems()]
