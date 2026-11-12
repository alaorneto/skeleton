from django.apps import AppConfig
from django.utils.translation import gettext_lazy


class JobsConfig(AppConfig):
    name = "skeleton.jobs"
    label = "sk_jobs"
    verbose_name = gettext_lazy("Jobs")
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import checks  # noqa: F401 - registra os system checks dos tipos de job
        from .retention import register_retention_job

        register_retention_job()
