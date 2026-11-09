from django.apps import AppConfig


class FilesConfig(AppConfig):
    name = "skeleton.files"
    label = "sk_files"
    verbose_name = "Arquivos"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import checks  # noqa: F401 - registra os checks de armazenamento
