"""Events (#69): authorization from the calendar, busy blocks, validation and CAS."""

from datetime import UTC, date, datetime, timedelta

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.contrib.calendar import services
from skeleton.contrib.calendar.models import Calendar, Event
from skeleton.contrib.calendar.policies import readable_events
from skeleton.contrib.calendar.service_api import event_info
from tools.security_fixtures import actor_tenant

START = datetime(2026, 12, 1, 12, 0, tzinfo=UTC)


def timed(**changes):
    return {
        "title": "Reunião",
        "description": "Pauta",
        "location": "Sala 1",
        "starts_at": START,
        "ends_at": START + timedelta(hours=1),
        **changes,
    }


def shared_calendar(agenda, **changes):
    with actor_tenant(agenda.alice, agenda.a):
        calendar = Calendar(organization_id=agenda.a.pk, owner=agenda.alice, name="Equipe", kind="shared", **changes)
        calendar.save()
    return calendar


def test_reading_the_calendar_reads_events_but_does_not_create(agenda):
    from skeleton.access.models import RecordGrant
    from skeleton.access.policies import table_definition
    from skeleton.tenancy.models import Membership

    calendar = shared_calendar(agenda)
    with actor_tenant(agenda.alice, agenda.a):
        event = services.create_event(actor=agenda.alice, calendar_id=calendar.pk, data=timed())
        # Bob gets a read grant on the calendar only.
        RecordGrant(
            organization_id=agenda.a.pk,
            table=table_definition(Calendar),
            record_sysid=calendar.pk,
            action="read",
            membership=Membership.objects.get(user=agenda.bob),
        ).save()
    with actor_tenant(agenda.bob, agenda.a):
        assert list(readable_events(agenda.bob).values_list("pk", flat=True)) == [event.pk]
        with pytest.raises(PermissionDenied):
            services.create_event(actor=agenda.bob, calendar_id=calendar.pk, data=timed())
        with pytest.raises(PermissionDenied):
            services.update_event(actor=agenda.bob, event_id=event.pk, data={"title": "x"}, expected_revision=1)
    with actor_tenant(agenda.bob, agenda.b):
        assert not readable_events(agenda.bob).filter(pk=event.pk).exists()  # Other organization.


def test_private_events_are_busy_blocks_for_readers(agenda):
    calendar = shared_calendar(agenda, visibility="organization")
    with actor_tenant(agenda.alice, agenda.a):
        private = services.create_event(actor=agenda.alice, calendar_id=calendar.pk, data=timed(is_private=True))
        public = services.create_event(actor=agenda.alice, calendar_id=calendar.pk, data=timed(title="Aberto"))
        own = event_info(agenda.alice, private)
        assert (own.title, own.busy_only) == ("Reunião", False)
    with actor_tenant(agenda.bob, agenda.a):
        rows = {event.pk: event_info(agenda.bob, event) for event in readable_events(agenda.bob)}
    assert (rows[private.pk].title, rows[private.pk].description, rows[private.pk].location) == ("Ocupado", "", "")
    assert rows[private.pk].busy_only and rows[private.pk].starts_at == START
    assert rows[public.pk].title == "Aberto" and not rows[public.pk].busy_only


def test_admin_sees_only_busy_blocks_in_someone_elses_personal_calendar(agenda):
    with actor_tenant(agenda.alice, agenda.a):
        personal = services.ensure_personal_calendar(agenda.alice)
        event = services.create_event(actor=agenda.alice, calendar_id=personal.pk, data=timed())
    with actor_tenant(agenda.admin, agenda.a):
        [seen] = [event_info(agenda.admin, row) for row in readable_events(agenda.admin).filter(pk=event.pk)]
        assert (seen.title, seen.busy_only) == ("Ocupado", True)
        with pytest.raises(PermissionDenied):  # Admin reads (busy) but writing still needs update.
            services.update_event(actor=agenda.admin, event_id=event.pk, data={"title": "x"}, expected_revision=1)


def test_time_validation_and_all_day_dates(agenda):
    calendar = shared_calendar(agenda)
    with actor_tenant(agenda.alice, agenda.a):
        for bad in (timed(ends_at=START), timed(ends_at=START - timedelta(minutes=1)), timed(tzid="Nowhere/City")):
            with pytest.raises(ValidationError):
                services.create_event(actor=agenda.alice, calendar_id=calendar.pk, data=bad)
        day = services.create_event(
            actor=agenda.alice,
            calendar_id=calendar.pk,
            data={
                "title": "Feriado",
                "all_day": True,
                "start_date": date(2026, 12, 25),
                "end_date": date(2026, 12, 26),
            },
        )
        stored = Event.objects.get(pk=day.pk)
        assert (stored.start_date, stored.end_date, stored.starts_at) == (date(2026, 12, 25), date(2026, 12, 26), None)
        with pytest.raises(ValidationError):
            services.create_event(
                actor=agenda.alice,
                calendar_id=calendar.pk,
                data={"title": "x", "all_day": True, "start_date": date(2026, 12, 25), "end_date": date(2026, 12, 25)},
            )
        with pytest.raises(ValidationError):
            services.create_event(actor=agenda.alice, calendar_id=calendar.pk, data=timed(organizer="x"))


def test_cas_has_one_winner_and_sequence_grows(agenda):
    calendar = shared_calendar(agenda)
    with actor_tenant(agenda.alice, agenda.a):
        event = services.create_event(actor=agenda.alice, calendar_id=calendar.pk, data=timed())
        changed = services.update_event(
            actor=agenda.alice, event_id=event.pk, data={"title": "Nova"}, expected_revision=event.revision
        )
        assert changed.sequence == 1
        with pytest.raises(ConcurrentUpdate):
            services.update_event(
                actor=agenda.alice, event_id=event.pk, data={"title": "Perdida"}, expected_revision=event.revision
            )
        cancelled = services.cancel_event(actor=agenda.alice, event_id=event.pk, expected_revision=changed.revision)
        assert cancelled.status == "cancelled"
        assert Event.objects.get(pk=event.pk).title == "Nova"
