from django.apps import AppConfig
from django.utils.translation import gettext_lazy


class AccessConfig(AppConfig):
    name = "skeleton.access"
    label = "sk_access"
    verbose_name = gettext_lazy("Controle de acesso")
    default_auto_field = "django.db.models.BigAutoField"
