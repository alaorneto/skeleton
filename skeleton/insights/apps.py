from django.apps import AppConfig
from django.utils.translation import gettext_lazy


class InsightsConfig(AppConfig):
    name = "skeleton.insights"
    label = "sk_insights"
    verbose_name = gettext_lazy("Indicadores da instância")
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from .jobs import register_insights

        register_insights()
