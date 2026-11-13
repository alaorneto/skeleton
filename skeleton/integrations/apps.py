from django.apps import AppConfig
from django.utils.translation import gettext_lazy


class IntegrationsConfig(AppConfig):
    name = "skeleton.integrations"
    label = "sk_integrations"
    verbose_name = gettext_lazy("Integrações")
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from .webhooks.delivery import register_webhook_delivery

        register_webhook_delivery()
