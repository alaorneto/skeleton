from django.apps import AppConfig


class JobsConfig(AppConfig):
    name = "skeleton.jobs"
    label = "sk_jobs"
    verbose_name = "Jobs"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import checks  # noqa: F401 - registra os system checks dos tipos de job
