from django.apps import AppConfig
from django.utils.translation import gettext_lazy


class TransferConfig(AppConfig):
    """CSV export and import of business records by background job (#182, #183)."""

    name = "skeleton.transfer"
    label = "sk_transfer"
    verbose_name = gettext_lazy("Exportação e importação")
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from .jobs import register_transfer_jobs

        register_transfer_jobs()
