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
from uuid import uuid4

from django.apps import apps
from django.db import connection, models

from skeleton.common.migration_operations import InstallRecordInfrastructure, build_infrastructure_specification
from skeleton.common.models import OrganizationRecord, SealableMixin, SecuredRecord
from skeleton.system import registry
from skeleton.tenancy.context import system_context

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


def grant(scenario, workspace, *, user, action, organization=None):
    from skeleton.access.models import RecordGrant
    from skeleton.system.models import TableDefinition
    from skeleton.tenancy.models import Membership

    organization = organization or scenario.a
    with system_context(reason="fixture:derived-grant", organization_id=organization.pk):
        membership = Membership.objects.get(user=user, organization=organization)
        table = TableDefinition.objects.get(model_label=workspace._meta.label_lower)
        RecordGrant(
            organization=organization, table=table, record_sysid=workspace.pk, membership=membership, action=action
        ).save()


def reader_only(scenario, model):
    """A member of A whose role has only the read permission on the anchor table."""
    from allauth.account.models import EmailAddress

    from skeleton.access.models import Role, TablePermission
    from skeleton.access.services import provision_membership
    from skeleton.accounts.services import create_identity
    from skeleton.system.models import TableDefinition

    with system_context(reason="fixture:derived-identity"):
        user = create_identity(email=f"read-only-{uuid4().hex[:8]}@derived.example.test", password=scenario.password)
        EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)
    with system_context(reason="fixture:derived-role", organization_id=scenario.a.pk):
        role = Role(organization=scenario.a, code="derived-reader", name="Derived reader")
        role.save()
        table = TableDefinition.objects.get(model_label=model._meta.label_lower)
        TablePermission(organization=scenario.a, role=role, table=table, action="read").save()
        provision_membership(organization=scenario.a, user=user, role=Role.objects.get(code="member"))
        provision_membership(organization=scenario.a, user=user, role=role)
    return user


def child(model, organization, **values):
    with system_context(reason="fixture:derived-child", organization_id=organization.pk):
        record = model(organization=organization, **values)
        record.save()
    return record


def tombstone(model, record, organization):
    """Tombstone a parent that still has live children, as only SQL can (the invariant is defensive)."""
    with system_context(reason="fixture:derived-tombstone", organization_id=organization.pk):
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE skeleton.{model._meta.db_table} SET deleted_at=now(), revision=revision+1 WHERE sysid=%s",
                [record.pk],
            )
