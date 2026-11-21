"""Writes to a global table declared by an app only in a system context without organization (#576).

``example_notes.notecategory`` is the global table (``SystemRecord``, scope ``global``) and
``load_note_categories`` the operator command that writes it through ``command_context``. Every
assertion runs on PostgreSQL with the runtime role.
"""

import io
from uuid import uuid4

import pytest
from django.core.management import call_command
from django.db import DatabaseError, connection, models, transaction

from skeleton.common.exceptions import SystemContextRequired
from skeleton.tenancy.context import InvalidContext, command_context, request_context, system_context
from tools.security_fixtures import actor_tenant, build_security_fixture

pytestmark = pytest.mark.needs_example_app

REASON = "system:command:example_notes:load_note_categories"


@pytest.fixture
def records(skeleton_db):
    assert connection.vendor == "postgresql", "The guard runs on PostgreSQL with the runtime role."
    return build_security_fixture()


def load(*entries):
    stdout = io.StringIO()
    call_command("load_note_categories", *entries, stdout=stdout)
    return stdout.getvalue()


def platform_admin():
    from skeleton.accounts.services import bootstrap_identity

    platform, _ = bootstrap_identity(
        email="global-tables-platform@example.test",
        organization="global-tables-platform",
        name="Platform",
        password="Kq9-unrelated-Phrase-2026!",
    )
    return platform


def test_the_operator_command_writes_through_the_public_path_and_is_audited(records):
    from examples.notes.models import NoteCategory
    from skeleton.audit.models import AuditEvent

    assert load("ADM=Administrativa", "OPS=Operação") == "2 created, 0 updated.\n"
    assert load("ADM=Administração", "OPS=Operação") == "0 created, 1 updated.\n"
    with system_context(reason="test:read-categories"):
        categories = {category.code: category for category in NoteCategory.objects.all()}
    assert {code: category.title for code, category in categories.items()} == {
        "ADM": "Administração",
        "OPS": "Operação",
    }
    platform = platform_admin()
    # Global audit events are readable only by the platform (skeleton_audit_select).
    with request_context(actor=platform, request_id=uuid4(), source="test:read-global-audit"):
        audit = list(
            AuditEvent.objects.filter(record_sysid=categories["ADM"].pk)
            .order_by("record_revision")
            .values("operation", "source", "organization_id", "actor_sysid")
        )
    assert [event["operation"] for event in audit] == ["create", "update"]
    assert {event["source"] for event in audit} == {REASON}
    assert {event["organization_id"] for event in audit} == {None}
    assert {event["actor_sysid"] for event in audit} == {None}


def test_writes_outside_a_system_context_without_organization_are_refused(records, subtests):
    """Model barrier: tenant, request and organization-bound system contexts all refuse (#576)."""
    from examples.notes.models import NoteCategory

    load("ADM=Administrativa")

    def tenant():
        return actor_tenant(records.alice, records.a)

    def request():
        return request_context(actor=records.alice, request_id=uuid4(), source="test:global-request")

    def organization_system():
        return system_context(reason="test:consumer-like", organization_id=records.a.pk)

    for name, opened in (("tenant", tenant), ("request", request), ("organization system", organization_system)):
        with subtests.test(context=name):
            with opened():
                with pytest.raises(SystemContextRequired):
                    NoteCategory(code="NEW", title="Nova").save()
                existing = NoteCategory.objects.get(code="ADM")
                existing.title = "Mudada"
                with pytest.raises(SystemContextRequired):
                    existing.save(update_fields=["title"])
                with pytest.raises(SystemContextRequired):
                    NoteCategory.objects.filter(code="ADM").update(title="Mudada")
                with pytest.raises(SystemContextRequired):
                    existing.delete()
    with system_context(reason="test:read-categories"):
        assert list(NoteCategory.objects.values_list("code", "title")) == [("ADM", "Administrativa")]


def test_the_frozen_guard_refuses_writes_that_skip_the_model(records, subtests):
    """Database barrier: an ORM update without full_clean and raw SQL fail inside PostgreSQL."""
    from examples.notes.models import NoteCategory

    load("ADM=Administrativa")
    with system_context(reason="test:read-categories"):
        category = NoteCategory.objects.get(code="ADM")
    table = connection.ops.quote_name(NoteCategory._meta.db_table)
    insert = (
        f"INSERT INTO {table} (sysid, created_at, updated_at, revision, code, title) "
        "VALUES (%s, now(), now(), 1, 'RAW', 'Raw')"
    )
    update = f"UPDATE {table} SET title='Raw', revision=revision+1 WHERE sysid=%s"

    def tenant():
        return actor_tenant(records.alice, records.a)

    def organization_system():
        return system_context(reason="test:consumer-like", organization_id=records.a.pk)

    for name, opened in (("tenant", tenant), ("organization system", organization_system)):
        with subtests.test(context=name), opened():
            with pytest.raises(DatabaseError, match="global table requires a system context"), transaction.atomic():
                models.QuerySet.update(NoteCategory.all_objects.filter(pk=category.pk), title="Raw")
            for statement, params in ((insert, [uuid4().hex]), (update, [category.pk.hex])):
                with pytest.raises(DatabaseError, match="global table requires a system context"):
                    with transaction.atomic(), connection.cursor() as cursor:
                        cursor.execute(statement, params)
    with transaction.atomic():
        with connection.cursor() as cursor:  # No context at all: the GUC is off as well.
            with pytest.raises(DatabaseError, match="global table requires a system context"):
                cursor.execute(update, [category.pk.hex])
    with system_context(reason="test:read-categories"):
        assert NoteCategory.objects.get(pk=category.pk).title == "Administrativa"
        assert not NoteCategory.objects.filter(code="RAW").exists()


def test_command_context_opens_only_at_the_top_of_a_unit_of_work(records, subtests):
    for name, opened in (
        ("tenant", lambda: actor_tenant(records.alice, records.a)),
        ("system", lambda: system_context(reason="test:job-like")),
        ("command", lambda: command_context(reason="test:outer")),
    ):
        with subtests.test(context=name), opened():
            with pytest.raises(InvalidContext, match="top of a management command"):
                with command_context(reason="test:nested"):
                    pass
    for reason in ("", "   ", None):
        with subtests.test(reason=reason), pytest.raises(InvalidContext, match="requires a reason"):
            with command_context(reason=reason):
                pass
    with command_context(reason="test:top") as active:
        assert active.system and active.organization_id is None and active.actor is None
        assert active.source == "system:command:test:top"


def test_core_global_tables_keep_their_service_writes(records):
    """Out of scope (#576): a core global table is still written by its service in a request context."""
    from skeleton.system import availability
    from skeleton.system.models import AppAvailability

    platform = platform_admin()
    with request_context(actor=platform, request_id=uuid4(), source="test:core-global"):
        current = AppAvailability.objects.filter(app_label="example_notes").first()
        revision = current.revision if current else 0
        availability.set_app_availability("example_notes", user=platform, mode="selected", revision=revision)
        assert AppAvailability.objects.get(app_label="example_notes").mode == "selected"


def test_the_schema_check_reports_the_guard_reinstalled_without_system_writes(records, owner_schema_editor):
    """Deployment check: positive with the frozen guard, negative once the system-write clause is gone."""
    from examples.notes.models import NoteCategory
    from skeleton.common.migration_operations import InstallRecordInfrastructure
    from skeleton.system.checks import schema

    table = NoteCategory._meta.db_table

    def guard_errors():
        return [error for error in schema.check_deployed_schema() if error.startswith(f"{table}:") and "guard" in error]

    frozen = schema._frozen_operations(from_migrations=True)[0]["example_notes.notecategory"].specification
    assert guard_errors() == []
    without = {key: value for key, value in frozen.items() if key != "writes"}
    with owner_schema_editor() as editor:
        InstallRecordInfrastructure("example_notes", "notecategory", without)._install_guard(editor, NoteCategory)
    assert guard_errors() == [
        f"{table}: exact guard function body/owner/security/search_path/EXECUTE inventory mismatch."
    ]
