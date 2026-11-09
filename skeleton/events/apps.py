from django.apps import AppConfig


class EventsConfig(AppConfig):
    name = "skeleton.events"
    label = "sk_events"
    verbose_name = "Eventos de domínio"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import checks  # noqa: F401 - registra os system checks dos tipos de evento
