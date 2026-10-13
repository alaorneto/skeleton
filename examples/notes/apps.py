from django.apps import AppConfig


class NotesConfig(AppConfig):
    name = __package__
    label = "example_notes"
    verbose_name = "Anotações de demonstração"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from skeleton.system.registry import TableSpec, register
        from pathlib import Path
        from skeleton.system.app_registry import AppManifest, register_app
        register_app(AppManifest.from_json(Path(self.path) / "manifest.json", app_config=self))
        from .models import Note

        register(TableSpec(model=Note, description="Anotações de demonstração do contrato de registros", label="Anotações", scope="organization", read_fields=("code", "title", "body", "owner", "visibility", "owning_group"), write_fields=("code", "title", "body"), sensitive_fields=("body",), expose_api=True))
