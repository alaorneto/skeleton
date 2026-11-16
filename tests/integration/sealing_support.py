"""A sealable probe table of the example app, shared by the seal tests (#527, #529).

The model joins the global registry under ``example_notes`` (catalog, table permissions and app
activation apply as to any app table) and leaves it afterwards; the owner creates its table. Every
module that imports this one is marked ``needs_example_app``.
"""

from contextlib import contextmanager

from django.apps import apps
from django.db import models

from skeleton.common.migration_operations import InstallRecordInfrastructure, build_infrastructure_specification
from skeleton.common.models import SealableMixin, SecuredRecord
from skeleton.system import registry

TABLE = "example_notes.sealeddocument"


@contextmanager
def sealed_document_table(owner_schema_editor, **policies):
    attributes = {
        "__module__": "examples.notes.models",
        "title": models.CharField(max_length=100, blank=True, default=""),
        "body": models.TextField(blank=True, default=""),
        "status": models.CharField(max_length=20, blank=True, default="draft"),
        "seal_fields": ("body",),
        "Meta": type(
            "Meta", (SecuredRecord.Meta,), {"app_label": "example_notes", "db_table": "sk_test_sealeddocument"}
        ),
    }
    model = type("SealedDocument", (SealableMixin, SecuredRecord), attributes)
    try:
        spec = registry.TableSpec(
            model=model,
            description="Documento selável de teste",
            read_fields=("title", "body", "status"),
            write_fields=("title", "body", "status"),
            expose_api=True,
            seal=policies.pop("seal", registry.SealPolicy()),
            **policies,
        )
        registry.register(spec)
        specification = build_infrastructure_specification(model, spec)
        with owner_schema_editor() as editor:
            editor.create_model(model)
            InstallRecordInfrastructure("example_notes", "sealeddocument", specification)._postgres(editor, model)
        yield model
    finally:
        registry._registry.pop(TABLE, None)
        apps.all_models["example_notes"].pop("sealeddocument", None)
        apps.clear_cache()
