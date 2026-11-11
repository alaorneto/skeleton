from django.apps import AppConfig


class EventsConfig(AppConfig):
    name = "skeleton.events"
    label = "sk_events"
    verbose_name = "Eventos de domínio"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import checks  # noqa: F401 - registra os system checks dos tipos de evento
        from .consumers import register_event_jobs

        register_event_jobs()
        from .core_events import register_core_event_types

        register_core_event_types()
        from datetime import timedelta

        from skeleton.jobs.periodic import register_periodic

        register_periodic("events.retention", "events.retention", every=timedelta(hours=6))
