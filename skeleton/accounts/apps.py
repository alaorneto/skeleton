from django.apps import AppConfig
from django.utils.translation import gettext_lazy


class AccountsConfig(AppConfig):
    name = "skeleton.accounts"
    label = "sk_accounts"
    verbose_name = gettext_lazy("Identidade")
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import signals  # noqa: F401
