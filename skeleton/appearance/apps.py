from django.apps import AppConfig
from django.utils.translation import gettext_lazy


class AppearanceConfig(AppConfig):
    name = "skeleton.appearance"
    label = "sk_appearance"
    verbose_name = gettext_lazy("Aparência da instância")
    default_auto_field = "django.db.models.BigAutoField"
