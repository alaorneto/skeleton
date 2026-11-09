from django.core.checks import Error, register


@register()
def job_type_checks(app_configs=None, **kwargs):
    from .registry import job_type_problems

    return [Error(problem, id="skeleton.E021") for problem in job_type_problems()]
