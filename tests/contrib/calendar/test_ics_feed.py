"""ICS export and feed (#76): RFC 5545 output, current authorization, busy blocks, token lifecycle."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from icalendar import Calendar as ICalendar

from skeleton.contrib.calendar import services
from skeleton.contrib.calendar.models import Calendar
from skeleton.service_api.tokens import FeedTokenService
from skeleton.tenancy.context import request_context
from tools.security_fixtures import actor_tenant

START = datetime(2027, 1, 4, 9, 0, tzinfo=UTC)


@pytest.fixture
def series(agenda):
    with actor_tenant(agenda.alice, agenda.a):
        calendar = Calendar(
            organization_id=agenda.a.pk, owner=agenda.alice, name="Time", kind="shared", visibility="organization"
        )
        calendar.save()
        event = services.create_event(
            actor=agenda.alice,
            calendar_id=calendar.pk,
            data={
                "title": "Semanal",
                "description": "Pauta",
                "starts_at": START,
                "ends_at": START + timedelta(hours=1),
                "tzid": "Europe/Berlin",
                "rrule": "FREQ=WEEKLY;COUNT=4",
            },
        )
        second, third = START + timedelta(days=7), START + timedelta(days=14)
        event = services.cancel_occurrence(
            actor=agenda.alice, event_id=event.pk, recurrence_id=second, expected_revision=event.revision
        )
        services.edit_occurrence(
            actor=agenda.alice,
            event_id=event.pk,
            recurrence_id=third,
            mode="this",
            data={"title": "Movida", "starts_at": third + timedelta(hours=1), "ends_at": third + timedelta(hours=2)},
            expected_revision=event.revision,
        )
        services.create_event(
            actor=agenda.alice,
            calendar_id=calendar.pk,
            data={"title": "Sigilo", "starts_at": START, "ends_at": START + timedelta(minutes=30), "is_private": True},
        )
    return calendar


def components(body):
    return [component for component in ICalendar.from_ical(body).walk() if component.name == "VEVENT"]


def issue(user, *, scope="calendar.feed", expires=timedelta(days=30)):
    with request_context(actor=user, request_id=uuid4(), source="test:feed"):
        return FeedTokenService().issue(actor=user, name="Feed", scope=scope, expires_at=timezone.now() + expires)


def test_download_is_valid_rfc5545_with_rrule_exdate_and_recurrence_id(agenda, login, series):
    url = reverse("skeleton_calendar:calendar-ics", kwargs={"organization_id": agenda.a.pk, "sysid": series.pk})
    response = login(agenda.alice).get(url)
    assert response.status_code == 200 and response["Content-Type"].startswith("text/calendar")
    events = components(response.content)
    master = next(item for item in events if "RRULE" in item)
    assert master["RRULE"]["FREQ"] == ["WEEKLY"] and master["RRULE"]["COUNT"] == [4]
    assert master.get("EXDATE") is not None
    override = next(item for item in events if "RECURRENCE-ID" in item)
    assert str(override["UID"]) == str(master["UID"]) and str(override["SUMMARY"]) == "Movida"
    private = next(item for item in events if str(item["SUMMARY"]) in ("Sigilo", "Ocupado") and "RRULE" not in item)
    assert str(private["SUMMARY"]) == "Sigilo"  # Alice organizes it.


def test_feed_respects_current_authorization_and_busy_blocks(agenda, series):
    issued = issue(agenda.bob)
    url = f"/me/apps/skeleton_calendar/feed/{issued.text}.ics"
    body = Client().get(url).content
    summaries = [str(item["SUMMARY"]) for item in components(body)]
    assert "Semanal" in summaries and "Sigilo" not in summaries and "Ocupado" in summaries
    assert not any("DESCRIPTION" in item for item in components(body) if str(item["SUMMARY"]) == "Ocupado")
    busy = [str(item["SUMMARY"]) for item in components(Client().get(url + "?busy=1").content)]
    assert set(busy) == {"Ocupado"}
    # Revoke Bob's table permission in A: the next fetch has nothing of A.
    from skeleton.access.models import Role, TablePermission
    from skeleton.access.policies import table_definition

    with actor_tenant(agenda.admin, agenda.a):
        member = Role.objects.get(code="member", is_builtin=True)
        TablePermission.objects.get(role=member, table=table_definition(Calendar), action="read").delete()
    assert components(Client().get(url).content) == []


def test_revoked_expired_or_foreign_tokens_answer_404(agenda, series, monkeypatch):
    from skeleton.integrations.tokens import services as token_services
    from skeleton.integrations.tokens.services import revoke_token

    issued = issue(agenda.bob)
    url = f"/me/apps/skeleton_calendar/feed/{issued.text}.ics"
    assert Client().get(url).status_code == 200
    with request_context(actor=agenda.bob, request_id=uuid4(), source="test:feed"):
        revoke_token(actor=agenda.bob, token_id=issued.sysid, expected_revision=1)
    assert Client().get(url).status_code == 404
    other = issue(agenda.alice)
    later = timezone.now() + timedelta(days=31)
    from types import SimpleNamespace

    monkeypatch.setattr(token_services, "timezone", SimpleNamespace(now=lambda: later))
    assert Client().get(f"/me/apps/skeleton_calendar/feed/{other.text}.ics").status_code == 404
    monkeypatch.undo()
    assert Client().get("/me/apps/skeleton_calendar/feed/skt_00000000_" + "A" * 43 + ".ics").status_code == 404
    # A feed token is not a Bearer credential for the API.
    api = Client(HTTP_AUTHORIZATION=f"Bearer {other.text}").get(reverse("api:me"))
    assert api.status_code == 401


def test_feed_token_page_shows_the_url_once(agenda, login):
    client = login(agenda.alice)
    url = reverse("skeleton_calendar_me:feed-token")
    assert client.get(url).status_code == 200
    created = client.post(url, {"csrfmiddlewaretoken": client.cookies["csrftoken"].value})
    assert created.status_code == 201 and "no-store" in created["Cache-Control"]
    assert "/me/apps/skeleton_calendar/feed/skt_" in created.content.decode()


def test_feed_is_windowed_and_capped(agenda, series, monkeypatch):
    """Ended over 30 days ago and single events beyond 400 days stay out; the file stays valid (#291)."""
    from skeleton.contrib.calendar import ics

    now = timezone.now()
    with actor_tenant(agenda.alice, agenda.a):
        calendar = Calendar.objects.get(pk=series.pk)
        for title, moment in (("Antigo", now - timedelta(days=40)), ("Distante", now + timedelta(days=500))):
            services.create_event(
                actor=agenda.alice,
                calendar_id=calendar.pk,
                data={"title": title, "starts_at": moment, "ends_at": moment + timedelta(hours=1)},
            )
        recent = now + timedelta(days=2)
        services.create_event(
            actor=agenda.alice,
            calendar_id=calendar.pk,
            data={"title": "Próximo", "starts_at": recent, "ends_at": recent + timedelta(hours=1)},
        )
    url = f"/me/apps/skeleton_calendar/feed/{issue(agenda.bob).text}.ics"
    summaries = [str(item["SUMMARY"]) for item in components(Client().get(url).content)]
    assert "Próximo" in summaries and "Antigo" not in summaries and "Distante" not in summaries
    monkeypatch.setattr(ics, "MAX_COMPONENTS", 2)
    assert len(components(Client().get(url).content)) == 2


def test_export_query_count_is_constant(agenda, login, series):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    url = reverse("skeleton_calendar:calendar-ics", kwargs={"organization_id": agenda.a.pk, "sysid": series.pk})
    client = login(agenda.bob)

    def measured():
        with CaptureQueriesContext(connection) as queries:
            assert client.get(url).status_code == 200
        return len(queries)

    few = measured()
    with actor_tenant(agenda.alice, agenda.a):
        for index in range(10):
            moment = START + timedelta(days=30 + index)
            services.create_event(
                actor=agenda.alice,
                calendar_id=series.pk,
                data={
                    "title": f"Série {index}",
                    "starts_at": moment,
                    "ends_at": moment + timedelta(hours=1),
                    "rrule": "FREQ=DAILY;COUNT=3",
                },
            )
    assert measured() == few


# --------------------------------------------------------------------------- local time of series (#293)


def _shared_calendar(agenda):
    with actor_tenant(agenda.alice, agenda.a):
        calendar = Calendar(
            organization_id=agenda.a.pk, owner=agenda.alice, name="Berlim", kind="shared", visibility="organization"
        )
        calendar.save()
    return calendar


def _export(agenda, login, calendar):
    url = reverse("skeleton_calendar:calendar-ics", kwargs={"organization_id": agenda.a.pk, "sysid": calendar.pk})
    response = login(agenda.alice).get(url)
    assert response.status_code == 200
    return response.content


def test_ics_recurring_event_keeps_local_time_across_dst(agenda, login):
    from zoneinfo import ZoneInfo

    from dateutil.rrule import rrulestr

    from skeleton.contrib.calendar.service_api import CalendarService

    berlin = ZoneInfo("Europe/Berlin")
    calendar = _shared_calendar(agenda)
    start = datetime(2027, 3, 15, 9, 0, tzinfo=berlin)
    with actor_tenant(agenda.alice, agenda.a):
        services.create_event(
            actor=agenda.alice,
            calendar_id=calendar.pk,
            data={
                "title": "Semanal em Berlim",
                "starts_at": start,
                "ends_at": start + timedelta(hours=1),
                "tzid": "Europe/Berlin",
                "rrule": "FREQ=WEEKLY;COUNT=4",
            },
        )
        expected = [
            occurrence.starts_at
            for occurrence in CalendarService().upcoming(
                actor=agenda.alice, start=start - timedelta(days=1), end=start + timedelta(days=60)
            )
        ]
    [vevent] = components(_export(agenda, login, calendar))
    dtstart = vevent["DTSTART"].dt
    assert str(dtstart.tzinfo) == "Europe/Berlin" and (dtstart.hour, dtstart.minute) == (9, 0)
    rule = vevent["RRULE"].to_ical().decode()
    starts = [
        moment.replace(tzinfo=dtstart.tzinfo).astimezone(UTC)
        for moment in rrulestr(rule, dtstart=dtstart.replace(tzinfo=None))
    ]
    assert starts == expected and len(starts) == 4


def test_ics_until_is_exported_in_utc(agenda, login):
    calendar = _shared_calendar(agenda)
    start = datetime(2027, 3, 15, 8, 0, tzinfo=UTC)
    with actor_tenant(agenda.alice, agenda.a):
        services.create_event(
            actor=agenda.alice,
            calendar_id=calendar.pk,
            data={
                "title": "Diária com hora",
                "starts_at": start,
                "ends_at": start + timedelta(hours=1),
                "tzid": "Europe/Berlin",
                "rrule": "FREQ=DAILY;UNTIL=20270320",
            },
        )
        services.create_event(
            actor=agenda.alice,
            calendar_id=calendar.pk,
            data={
                "title": "Diária de dia inteiro",
                "all_day": True,
                "start_date": start.date(),
                "end_date": start.date() + timedelta(days=1),
                "rrule": "FREQ=DAILY;UNTIL=20270320",
            },
        )
    rules = {
        str(item["SUMMARY"]): item["RRULE"].to_ical().decode() for item in components(_export(agenda, login, calendar))
    }
    timed_until = rules["Diária com hora"].split("UNTIL=")[1].split(";")[0]
    day_until = rules["Diária de dia inteiro"].split("UNTIL=")[1].split(";")[0]
    assert timed_until.endswith("Z") and len(timed_until) == 16
    assert day_until == "20270320"


def test_ics_exdate_and_recurrence_id_are_the_stored_instants(agenda, login, series):
    from skeleton.contrib.calendar.models import Event
    from skeleton.contrib.calendar.recurrence import _instants

    events = components(_export(agenda, login, series))
    master = next(item for item in events if "RRULE" in item)
    exported = []
    for prop in master.get("EXDATE") if isinstance(master.get("EXDATE"), list) else [master.get("EXDATE")]:
        exported.extend(value.dt.astimezone(UTC) for value in prop.dts)
    override = next(item for item in events if "RECURRENCE-ID" in item)
    with actor_tenant(agenda.alice, agenda.a):
        stored = Event.objects.get(uid=str(master["UID"]), series__isnull=True)
        moved = Event.objects.get(series=stored)
        assert sorted(exported) == sorted(_instants(stored.exdates, stored))
        assert override["RECURRENCE-ID"].dt.astimezone(UTC) == moved.recurrence_id


def test_ics_contains_the_timezone_definition(agenda, login, series):
    body = _export(agenda, login, series)
    assert b"BEGIN:VTIMEZONE" in body and b"TZID:Europe/Berlin" in body
