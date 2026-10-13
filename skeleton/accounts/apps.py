from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "skeleton.accounts"
    label = "sk_accounts"
    verbose_name = "Identidade"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import signals  # noqa: F401
