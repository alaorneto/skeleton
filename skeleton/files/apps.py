from django.apps import AppConfig
from django.utils.translation import gettext_lazy


class FilesConfig(AppConfig):
    name = "skeleton.files"
    label = "sk_files"
    verbose_name = gettext_lazy("Arquivos")
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import checks  # noqa: F401 - registra os checks de armazenamento
        from .jobs import register_file_jobs

        register_file_jobs()
        from skeleton.integrations.tokens.scopes import register_token_scope

        # Attachments through the API also need the record's table scope (#46).
        register_token_scope("files:read", gettext_lazy("Listar anexos dos registros"))
        register_token_scope("files:write", gettext_lazy("Enviar, remover e restaurar anexos"))
