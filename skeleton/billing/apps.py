from django.apps import AppConfig
from django.utils.translation import gettext_lazy


class BillingConfig(AppConfig):
    name = "skeleton.billing"
    label = "sk_billing"
    verbose_name = gettext_lazy("Cobrança")
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import checks  # noqa: F401 - registers the billing checks
