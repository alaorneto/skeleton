from django.apps import AppConfig


class SequencesConfig(AppConfig):
    name = "skeleton.sequences"
    label = "sk_sequences"
    verbose_name = "Sequências"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import checks  # noqa: F401 - registers the sequence checks
