from django.apps import AppConfig


class TenancyConfig(AppConfig):
    name = "skeleton.tenancy"
    label = "sk_tenancy"
    verbose_name = "Organizações"
    default_auto_field = "django.db.models.BigAutoField"
