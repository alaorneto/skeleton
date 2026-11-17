"""Workspace/WorkItem/WorkItemLine: a container and its derived tables, shared by the tests of epic #560.

``Workspace`` is an exposed ``SecuredRecord`` (the anchor). ``WorkItem`` is an unexposed
``OrganizationRecord`` with ``access_parent="workspace"`` and ``WorkItemLine`` one more hop below,
with ``access_parent="item"``. The models are the ones of the recipe in docs/extending.md
("Contêiner e tabelas derivadas"). They join the global registry under ``example_notes``
(catalog, table permissions and app activation apply as to any app table) and leave it
afterwards; the owner creates the tables. Every module that imports this one is marked
``needs_example_app``.

``sealable=True`` makes ``WorkItem`` a sealable record (``seal_fields=("title",)``) and its lines
children frozen by that seal (``seal_parent="item"``), for the seal tests (#564).
"""

from contextlib import contextmanager
from types import SimpleNamespace

from django.apps import apps
from django.db import models

from skeleton.common.migration_operations import InstallRecordInfrastructure, build_infrastructure_specification
from skeleton.common.models import OrganizationRecord, SealableMixin, SecuredRecord
from skeleton.system import registry

APP = "example_notes"
TABLES = (f"{APP}.workspace", f"{APP}.workitem", f"{APP}.workitemline")


def _meta(base, table):
    return type("Meta", (base.Meta,), {"app_label": APP, "db_table": table})


@contextmanager
def derived_tables(owner_schema_editor, *, item_spec=None, sealable=False):
    """Create and register the three tables; ``item_spec`` adds TableSpec options to ``WorkItem``."""
    workspace = type(
        "Workspace",
        (SecuredRecord,),
        {
            "__module__": "examples.notes.models",
            "title": models.CharField(max_length=100),
            "Meta": _meta(SecuredRecord, "sk_test_workspace"),
        },
    )
    item_attributes = {
        "__module__": "examples.notes.models",
        "workspace": models.ForeignKey(workspace, on_delete=models.PROTECT, related_name="items"),
        "title": models.CharField(max_length=100),
        "status": models.CharField(max_length=20, default="open"),
        "immutable_fields": ("workspace_id",),
        "Meta": _meta(OrganizationRecord, "sk_test_workitem"),
    }
    if sealable:
        item_attributes["seal_fields"] = ("title",)
    item = type("WorkItem", (SealableMixin, OrganizationRecord) if sealable else (OrganizationRecord,), item_attributes)
    line_attributes = {
        "__module__": "examples.notes.models",
        "item": models.ForeignKey(item, on_delete=models.PROTECT, related_name="lines"),
        "text": models.TextField(default=""),
        "immutable_fields": ("item_id",),
        "Meta": _meta(OrganizationRecord, "sk_test_workitemline"),
    }
    if sealable:
        line_attributes["seal_parent"] = "item"
    line = type("WorkItemLine", (OrganizationRecord,), line_attributes)
    specs = (
        registry.TableSpec(
            model=workspace,
            description="Área de trabalho.",
            read_fields=("title",),
            write_fields=("title",),
            expose_api=True,
        ),
        registry.TableSpec(
            model=item,
            description="Item de uma área de trabalho.",
            read_fields=("workspace", "title", "status"),
            write_fields=(),
            expose_api=False,
            access_parent="workspace",
            **({"seal": registry.SealPolicy()} if sealable else {}),
            **(item_spec or {}),
        ),
        registry.TableSpec(
            model=line,
            description="Linha de um item.",
            read_fields=("item", "text"),
            write_fields=(),
            expose_api=False,
            access_parent="item",
        ),
    )
    try:
        for spec in specs:
            registry.register(spec)
        with owner_schema_editor() as editor:
            for spec in specs:
                editor.create_model(spec.model)
                specification = build_infrastructure_specification(spec.model, spec)
                InstallRecordInfrastructure(APP, spec.model._meta.model_name, specification)._postgres(
                    editor, spec.model
                )
        yield SimpleNamespace(workspace=workspace, item=item, line=line)
    finally:
        for key in TABLES:
            registry._registry.pop(key, None)
            apps.all_models[APP].pop(key.split(".")[1], None)
        apps.clear_cache()
