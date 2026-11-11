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
    assert api.status_code in (401, 403)


def test_feed_token_page_shows_the_url_once(agenda, login):
    client = login(agenda.alice)
    url = reverse("skeleton_calendar_me:feed-token")
    assert client.get(url).status_code == 200
    created = client.post(url, {"csrfmiddlewaretoken": client.cookies["csrftoken"].value})
    assert created.status_code == 201 and "no-store" in created["Cache-Control"]
    assert "/me/apps/skeleton_calendar/feed/skt_" in created.content.decode()
