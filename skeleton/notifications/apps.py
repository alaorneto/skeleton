from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    name = "skeleton.notifications"
    label = "sk_notifications"
    verbose_name = "Notificações"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import checks  # noqa: F401 - registra os system checks dos tipos de notificação
        from .services import register_notification_jobs

        register_notification_jobs()
