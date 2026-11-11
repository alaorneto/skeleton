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
        from .models import Calendar

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
