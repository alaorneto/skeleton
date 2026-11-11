from django.apps import AppConfig


class CalendarConfig(AppConfig):
    name = "skeleton.contrib.calendar"
    label = "skeleton_calendar"
    verbose_name = "Agenda"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from pathlib import Path

        from skeleton.system.app_registry import AppManifest, register_app
        from skeleton.system.record_operations import RecordOperations, register_record_operations
        from skeleton.system.registry import TableSpec, register

        register_app(AppManifest.from_json(Path(self.path) / "manifest.json", app_config=self))
        from . import services
        from .models import Calendar, Event, EventAttendee, EventReminder

        register(
            TableSpec(
                model=Calendar,
                description="Calendários pessoais e compartilhados da organização; eventos herdam a autorização deles.",
                label="Calendários",
                scope="organization",
                read_fields=("name", "kind", "is_default", "color", "timezone", "description", "owner", "visibility"),
                write_fields=("name", "color", "timezone", "description"),
                sensitive_fields=(),
                expose_api=True,
            )
        )
        register(
            TableSpec(
                model=Event,
                description="Eventos dos calendários; leitura e escrita derivadas do calendário.",
                label="Eventos",
                scope="organization",
                read_fields=(
                    "calendar",
                    "uid",
                    "all_day",
                    "starts_at",
                    "ends_at",
                    "start_date",
                    "end_date",
                    "tzid",
                    "is_private",
                    "status",
                    "organizer",
                    "sequence",
                ),
                write_fields=(),
                # Redacted in the audit: an administrator reading it must not see private content.
                sensitive_fields=("title", "description", "location"),
                expose_api=False,
            )
        )
        register(
            TableSpec(
                model=EventAttendee,
                description="Membros convidados para um evento e a resposta deles.",
                label="Participantes",
                scope="organization",
                read_fields=("event", "membership", "response", "optional"),
                write_fields=(),
                sensitive_fields=(),
                expose_api=False,
            )
        )
        register(
            TableSpec(
                model=EventReminder,
                description="Lembretes de evento em minutos antes de cada ocorrência.",
                label="Lembretes",
                scope="organization",
                read_fields=("event", "minutes_before", "channel_hint"),
                write_fields=(),
                sensitive_fields=(),
                expose_api=False,
            )
        )
        register_record_operations(
            "skeleton_calendar.calendar",
            RecordOperations(
                create=services.create_calendar_record,
                update=services.update_calendar_record,
                delete=services.delete_calendar_record,
                restore=services.restore_calendar_record,
            ),
        )
        from .registration import register_calendar_extensions

        register_calendar_extensions()
