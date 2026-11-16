from django.apps import AppConfig
from django.utils.translation import gettext_lazy


class LedgerConfig(AppConfig):
    name = "skeleton.contrib.ledger"
    label = "skeleton_ledger"
    verbose_name = gettext_lazy("Livro contábil")
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from pathlib import Path

        from skeleton.system.app_registry import AppManifest, register_app

        register_app(AppManifest.from_json(Path(self.path) / "manifest.json", app_config=self))
        from .registration import register_ledger_tables

        register_ledger_tables()
        from .recurrence import register_recurrences

        register_recurrences()
        from . import checks  # noqa: F401 - registers the system checks
