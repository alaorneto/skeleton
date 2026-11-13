from django.apps import AppConfig
from django.utils.translation import gettext_lazy


class TenancyConfig(AppConfig):
    name = "skeleton.tenancy"
    label = "sk_tenancy"
    verbose_name = gettext_lazy("Organizações")
    default_auto_field = "django.db.models.BigAutoField"
