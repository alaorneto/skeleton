"""The new-table checklist of AGENTS.md, proven for the four calendar tables (#320).

Runs on PostgreSQL with the runtime role (never the owner): fail-closed without a tenant, A/B
isolation, same-organization foreign keys and guards enforced by the database, audit with
redaction and nothing left after a rollback, and soft delete/restore keeping the identity.
Direct SQL appears only here, in tests, always with parameters.
"""

import json
from uuid import uuid4

import pytest
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.urls import reverse
from django.utils import timezone

from skeleton.contrib.calendar import attendees, reminders, services
from skeleton.contrib.calendar.models import Calendar, Event, EventAttendee, EventReminder
from tools.security_fixtures import actor_tenant

MODELS = (Calendar, Event, EventAttendee, EventReminder)
TITLE = "Consulta sigilosa 2027"


def world(agenda, organization=None, owner=None, invitee=None):
    """A personal calendar, an event, an invitation and a reminder, as the owner in the organization."""
    organization, owner = organization or agenda.a, owner or agenda.alice
    invitee = invitee or agenda.bob
    start = timezone.now().replace(microsecond=0) + timezone.timedelta(days=3)
    with actor_tenant(owner, organization):
        calendar = services.ensure_personal_calendar(owner)
        event = services.create_event(
            actor=owner,
            calendar_id=calendar.pk,
            data={
                "title": TITLE,
                "description": "Pauta reservada",
                "location": "Sala secreta",
                "starts_at": start,
                "ends_at": start + timezone.timedelta(hours=1),
            },
        )
        [attendee] = attendees.invite(actor=owner, event_id=event.pk, user_ids=(invitee.pk,))
        reminder = reminders.add_reminder(actor=owner, event_id=event.pk, minutes_before=15)
    return {Calendar: calendar, Event: event, EventAttendee: attendee, EventReminder: reminder}


def audit_of(agenda, model, sysid):
    from skeleton.audit.models import AuditEvent

    with actor_tenant(agenda.admin, agenda.a):
        return list(
            AuditEvent.objects.filter(model_label=model._meta.label_lower, record_sysid=sysid).order_by(
                "created_at", "sysid"
            )
        )


def test_event_audit_redacts_title_description_and_location(agenda):
    rows = world(agenda)
    event = rows[Event]
    with actor_tenant(agenda.alice, agenda.a):
        event.refresh_from_db()
        services.update_event(
            actor=agenda.alice, event_id=event.pk, data={"title": "Outro segredo"}, expected_revision=event.revision
        )
    events = audit_of(agenda, Event, event.pk)
    create = next(item for item in events if item.operation == "create")
    update = next(item for item in events if item.operation == "update" and "title" in item.changes)
    for item in (create, update):
        for name in ("title", "description", "location"):
            if name in item.changes:
                assert item.changes[name]["redacted"] is True, name
        serialized = json.dumps(item.changes, default=str)
        assert TITLE not in serialized and "Outro segredo" not in serialized and "Sala secreta" not in serialized
    assert create.changes["starts_at"]["redacted"] is False and create.changes["starts_at"]["new"]


def test_audit_rows_exist_for_attendee_and_reminder(agenda):
    rows = world(agenda)
    for model in (EventAttendee, EventReminder):
        [create] = [item for item in audit_of(agenda, model, rows[model].pk) if item.operation == "create"]
        assert create.organization_id == agenda.a.pk and create.actor_sysid == agenda.alice.pk


def test_rollback_leaves_no_event_and_no_audit(agenda):
    with actor_tenant(agenda.alice, agenda.a):
        calendar = services.ensure_personal_calendar(agenda.alice)
        start = timezone.now() + timezone.timedelta(days=5)
        with transaction.atomic():
            event = services.create_event(
                actor=agenda.alice,
                calendar_id=calendar.pk,
                data={"title": "Desfeito", "starts_at": start, "ends_at": start + timezone.timedelta(hours=1)},
            )
            transaction.set_rollback(True)
        assert not Event.objects.with_deleted().filter(pk=event.pk).exists()
    assert audit_of(agenda, Event, event.pk) == []


@pytest.mark.parametrize("model", MODELS, ids=lambda model: model.__name__)
def test_models_fail_closed_without_tenant_context(agenda, model):
    from skeleton.common.exceptions import MissingTenantContext

    world(agenda)
    with pytest.raises(MissingTenantContext):
        model.objects.count()


@pytest.mark.parametrize("model", MODELS, ids=lambda model: model.__name__)
def test_organization_b_sees_no_rows_of_a(agenda, model):
    rows = world(agenda)
    with actor_tenant(agenda.bob, agenda.b):
        assert not model.objects.filter(pk=rows[model].pk).exists()
        assert not model.objects.with_deleted().filter(pk=rows[model].pk).exists()


def _b_targets(agenda):
    """Records of B that a row of A must never reference."""
    from skeleton.tenancy.models import Membership

    rows = world(agenda, organization=agenda.b, owner=agenda.bob, invitee=agenda.alice)
    with actor_tenant(agenda.bob, agenda.b):
        membership = Membership.objects.get(user=agenda.bob)
    return rows, membership


def _copy_with(model, source_pk, column, value):
    """INSERT a copy of a row of A with ``column`` pointing elsewhere (new identity, revision 1)."""
    table = connection.ops.quote_name(model._meta.db_table)
    columns = [field.column for field in model._meta.concrete_fields]
    overrides = {"sysid": uuid4().hex, column: value, "revision": 1}
    if model is Event:
        overrides["uid"] = f"{uuid4()}@checklist"
        if column == "series_id":  # An override row: the shape constraint wants these (only the FK may fail).
            overrides.update({"recurrence_id": timezone.now(), "rrule": ""})
    if model is EventReminder:
        overrides["minutes_before"] = 999  # Unique per event: keep the copy apart from B's own reminder.
    select, params = [], []
    for name in columns:
        if name in overrides:
            select.append("%s")
            params.append(overrides[name])
        else:
            select.append(connection.ops.quote_name(name))
    names = ", ".join(connection.ops.quote_name(name) for name in columns)
    sql = f"INSERT INTO {table} ({names}) SELECT {', '.join(select)} FROM {table} WHERE sysid = %s"
    return sql, [*params, source_pk.hex]


@pytest.mark.parametrize(
    "model,column,target",
    [
        (Event, "calendar_id", "calendar"),
        (Event, "series_id", "event"),
        (EventAttendee, "event_id", "event"),
        (EventAttendee, "membership_id", "membership"),
        (EventReminder, "event_id", "event"),
    ],
)
def test_database_refuses_cross_tenant_references(agenda, model, column, target):
    rows = world(agenda)
    foreign_rows, foreign_membership = _b_targets(agenda)
    foreign = {
        "calendar": foreign_rows[Calendar].pk,
        "event": foreign_rows[Event].pk,
        "membership": foreign_membership.pk,
    }[target]
    sql, params = _copy_with(model, rows[model].pk, column, foreign.hex)
    with actor_tenant(agenda.alice, agenda.a):
        with pytest.raises(IntegrityError) as error, transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
    assert getattr(error.value.__cause__, "sqlstate", None) == "23503", error.value  # foreign_key_violation


@pytest.mark.parametrize(
    "model,column",
    [
        (Calendar, "kind"),
        (Event, "calendar_id"),
        (Event, "uid"),
        (Event, "organizer_id"),
        (EventAttendee, "event_id"),
        (EventAttendee, "membership_id"),
        (EventReminder, "event_id"),
    ],
)
def test_database_refuses_physical_delete_and_immutable_changes(agenda, model, column):
    rows = world(agenda)
    other = world(agenda, organization=agenda.a, owner=agenda.bob, invitee=agenda.alice)
    record = rows[model]
    table = connection.ops.quote_name(model._meta.db_table)
    value = {
        "kind": "shared",
        "uid": f"{uuid4()}@checklist",
        "calendar_id": other[Calendar].pk.hex,
        "organizer_id": agenda.bob.pk.hex,
        "event_id": other[Event].pk.hex,
        "membership_id": None,
    }[column]
    if column == "membership_id":
        from skeleton.tenancy.models import Membership

        with actor_tenant(agenda.admin, agenda.a):
            value = Membership.objects.get(user=agenda.admin).pk.hex
    with actor_tenant(agenda.alice, agenda.a):
        for statement, params in (
            (f"DELETE FROM {table} WHERE sysid = %s", [record.pk.hex]),
            (
                f"UPDATE {table} SET {connection.ops.quote_name(column)} = %s, revision = revision + 1 WHERE sysid = %s",
                [value, record.pk.hex],
            ),
        ):
            with pytest.raises(DatabaseError), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(statement, params)
        assert model.objects.filter(pk=record.pk).exists()


def test_soft_delete_and_restore_of_a_calendar_keep_identity(agenda):
    from django.core.exceptions import ValidationError

    with actor_tenant(agenda.alice, agenda.a):
        calendar = Calendar(organization_id=agenda.a.pk, owner=agenda.alice, name="Equipe", kind="shared")
        calendar.save()
        before = calendar.revision
        services.delete_calendar_record(calendar, user=agenda.alice)
        assert not Calendar.objects.filter(pk=calendar.pk).exists()
        deleted = Calendar.objects.with_deleted().get(pk=calendar.pk)
        services.restore_calendar_record(deleted, user=agenda.alice)
        restored = Calendar.objects.get(pk=calendar.pk)
        assert restored.pk == calendar.pk and restored.revision > before
        default = services.ensure_personal_calendar(agenda.alice)
        with pytest.raises(ValidationError):
            services.delete_calendar_record(default, user=agenda.alice)
    operations = {item.operation for item in audit_of(agenda, Calendar, calendar.pk)}
    assert {"create", "soft_delete", "restore"} <= operations


def test_calendar_gateway_default_deny(agenda, login):
    rows = world(agenda)
    calendar = rows[Calendar]
    detail = reverse(
        "api:record",
        kwargs={"organization_id": agenda.a.pk, "table_key": "skeleton_calendar.calendar", "sysid": calendar.pk},
    )
    listing_b = reverse(
        "api:records", kwargs={"organization_id": agenda.b.pk, "table_key": "skeleton_calendar.calendar"}
    )
    for user in (agenda.outsider,):
        client = login(user)
        assert client.get(detail).status_code == 404
        changed = client.patch(
            detail,
            data=json.dumps({"name": "Invadido"}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
            HTTP_IF_MATCH=f'"{calendar.revision}"',
        )
        assert changed.status_code == 404
    # Alice is a member of B without TablePermission on calendars there: the default deny.
    alice = login(agenda.alice)
    assert alice.get(listing_b).status_code == 403
    with actor_tenant(agenda.alice, agenda.a):
        assert Calendar.objects.get(pk=calendar.pk).name == calendar.name
