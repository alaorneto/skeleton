from django.apps import AppConfig
from django.utils.translation import gettext_lazy


class NotificationsConfig(AppConfig):
    name = "skeleton.notifications"
    label = "sk_notifications"
    verbose_name = gettext_lazy("Notificações")
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import checks  # noqa: F401 - registra os system checks dos tipos de notificação
        from .jobs import register_notification_jobs

        register_notification_jobs()
