from django.conf import settings
from django.core.checks import Error, Tags, register


@register(Tags.security, deploy=True)
def private_storage_checks(app_configs=None, **kwargs):
    from .storage import ALIAS, storage_problems

    storages = getattr(settings, "STORAGES", {}) or {}
    return [
        Error(problem, id="skeleton.E024")
        for problem in storage_problems(storages.get(ALIAS), static_root=getattr(settings, "STATIC_ROOT", None))
    ]
