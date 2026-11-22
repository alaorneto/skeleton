"""Subscription link of one calendar (#669): one token per person and calendar, authorization reread."""

import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from icalendar import Calendar as ICalendar

from skeleton.contrib.calendar import services
from skeleton.contrib.calendar.models import Calendar
from skeleton.service_api import NotFound
from skeleton.service_api.access import AccessService
from skeleton.service_api.tokens import FeedTokenService
from skeleton.tenancy.context import request_context
from skeleton.testing.service_api import assert_service_contract
from tools.security_fixtures import actor_tenant

SOON = (datetime.now(UTC) + timedelta(days=3)).replace(hour=10, minute=0, second=0, microsecond=0)
TABLE = "skeleton_calendar.calendar"


def calendar(agenda, name, *, visibility="organization", owner=None):
    with actor_tenant(owner or agenda.alice, agenda.a):
        row = Calendar(
            organization_id=agenda.a.pk, owner=owner or agenda.alice, name=name, kind="shared", visibility=visibility
        )
        row.save()
    return row


def event(agenda, row, title, **extra):
    with actor_tenant(agenda.alice, agenda.a):
        services.create_event(
            actor=agenda.alice,
            calendar_id=row.pk,
            data={"title": title, "starts_at": SOON, "ends_at": SOON + timedelta(hours=1), **extra},
        )


def link_page(agenda, row):
    return reverse("skeleton_calendar:calendar-link", kwargs={"organization_id": agenda.a.pk, "sysid": row.pk})


def issue_link(client, agenda, row):
    response = client.post(link_page(agenda, row), {"csrfmiddlewaretoken": client.cookies["csrftoken"].value})
    assert response.status_code == 201 and "no-store" in response["Cache-Control"]
    [url] = re.findall(r"https?://[^\s<\"]+/calendar-feed/skt_[^<\s\"]+\.ics", response.content.decode())
    return url.split("testserver", 1)[1]


def summaries(response):
    assert response.status_code == 200 and response["Content-Type"].startswith("text/calendar")
    assert "no-store" in response["Cache-Control"]
    return sorted(str(item["SUMMARY"]) for item in ICalendar.from_ical(response.content).walk("VEVENT"))


def token_of(path):
    return path.rsplit("/", 1)[1].removesuffix(".ics")


def test_the_token_service_keeps_its_contract():
    assert_service_contract(FeedTokenService)


def test_each_calendar_has_its_own_link_and_revoking_one_keeps_the_other(agenda, login):
    from skeleton.integrations.tokens.models import ApiToken
    from skeleton.integrations.tokens.services import revoke_token

    team, rooms = calendar(agenda, "Time"), calendar(agenda, "Salas")
    event(agenda, team, "Reunião do time")
    event(agenda, rooms, "Sala reservada")
    client = login(agenda.bob)
    assert client.get(link_page(agenda, team)).status_code == 200
    team_url, rooms_url = issue_link(client, agenda, team), issue_link(client, agenda, rooms)
    assert team_url != rooms_url
    assert summaries(Client().get(team_url)) == ["Reunião do time"]
    assert summaries(Client().get(rooms_url)) == ["Sala reservada"]
    with request_context(actor=agenda.bob, request_id=uuid4(), source="test:link"):
        tokens = {row.target_id: row for row in ApiToken.objects.filter(user=agenda.bob, kind="feed")}
        assert set(tokens) == {team.pk, rooms.pk}
        assert {row.bound_organization_id for row in tokens.values()} == {agenda.a.pk}
        assert tokens[team.pk].name == "Calendário: Time" and tokens[team.pk].scopes == ["calendar.link"]
        revoke_token(actor=agenda.bob, token_id=tokens[team.pk].pk, expected_revision=tokens[team.pk].revision)
    assert Client().get(team_url).status_code == 404
    assert summaries(Client().get(rooms_url)) == ["Sala reservada"]
    # The profile token list names the calendar, and each link is revoked on its own there.
    listed = client.get(reverse("integrations:personal-tokens")).content.decode()
    assert "Calendário: Salas" in listed


def test_the_link_masks_like_the_pages_and_busy_only(agenda, login):
    team = calendar(agenda, "Time")
    event(agenda, team, "Aberto", description="Pauta", location="Sala 1")
    event(agenda, team, "Sigilo", is_private=True)
    url = issue_link(login(agenda.bob), agenda, team)
    body = Client().get(url)
    assert summaries(body) == ["Aberto", "Ocupado"]
    busy = [item for item in ICalendar.from_ical(body.content).walk("VEVENT") if str(item["SUMMARY"]) == "Ocupado"]
    assert busy and all("DESCRIPTION" not in item and "LOCATION" not in item for item in busy)
    assert summaries(Client().get(url + "?busy=1")) == ["Ocupado", "Ocupado"]
    # The organizer (and the calendar owner) sees their own private event.
    assert summaries(Client().get(issue_link(login(agenda.alice), agenda, team))) == ["Aberto", "Sigilo"]


@pytest.mark.needs_example_app
def test_a_managed_event_leaves_no_source_in_the_link(agenda, login):
    from skeleton.contrib.calendar.service_api import CalendarService
    from skeleton.tenancy.context import system_context
    from skeleton.testing.applications import activate_app, open_app

    open_app("example_notes")
    activate_app("example_notes", agenda.a.pk)
    team = calendar(agenda, "Atendimentos")
    with system_context(reason="test:calendar-link", organization_id=agenda.a.pk):
        CalendarService().put_managed_event(
            actor=None,
            source_app="example_notes",
            source_key="appointment-7",
            calendar_id=team.pk,
            data={"title": "Atendimento", "starts_at": SOON, "ends_at": SOON + timedelta(hours=1), "is_private": False},
            source_path="apps/example_notes/appointments/7/",
        )
    response = Client().get(issue_link(login(agenda.bob), agenda, team))
    assert summaries(response) == ["Atendimento"]
    assert b"apps/example_notes" not in response.content and b"example_notes" not in response.content


def test_losing_read_drops_the_link_and_reading_again_restores_it(agenda, login):
    private = calendar(agenda, "Diretoria", visibility="private")
    event(agenda, private, "Conselho")
    with actor_tenant(agenda.admin, agenda.a):
        AccessService().grant_record_access(
            actor=agenda.admin, table_key=TABLE, record_id=private.pk, action="read", membership_user_id=agenda.bob.pk
        )
    url = issue_link(login(agenda.bob), agenda, private)
    assert summaries(Client().get(url)) == ["Conselho"]
    with actor_tenant(agenda.admin, agenda.a):
        AccessService().revoke_record_access(
            actor=agenda.admin, table_key=TABLE, record_id=private.pk, action="read", membership_user_id=agenda.bob.pk
        )
    assert Client().get(url).status_code == 404
    with actor_tenant(agenda.admin, agenda.a):
        AccessService().grant_record_access(
            actor=agenda.admin, table_key=TABLE, record_id=private.pk, action="read", membership_user_id=agenda.bob.pk
        )
    assert summaries(Client().get(url)) == ["Conselho"]  # The token itself was never revoked.


def test_the_link_token_opens_nothing_else(agenda, login):
    team = calendar(agenda, "Time")
    event(agenda, team, "Reunião do time")
    url = issue_link(login(agenda.bob), agenda, team)
    text = token_of(url)
    # Not the personal feed, not the API.
    assert Client().get(f"/me/apps/skeleton_calendar/feed/{text}.ics").status_code == 404
    assert Client(HTTP_AUTHORIZATION=f"Bearer {text}").get(reverse("api:me")).status_code == 401
    # A personal feed token does not open the route of the calendar link.
    with request_context(actor=agenda.bob, request_id=uuid4(), source="test:link"):
        personal = FeedTokenService().issue(
            actor=agenda.bob, name="Feed", scope="calendar.feed", expires_at=timezone.now() + timedelta(days=30)
        )
    assert Client().get(f"/me/apps/skeleton_calendar/calendar-feed/{personal.text}.ics").status_code == 404
    assert Client().get(f"/me/apps/skeleton_calendar/feed/{personal.text}.ics").status_code == 200


def test_unknown_expired_and_rate_limited_links_answer_404(agenda, login, monkeypatch):
    from django.test import override_settings

    from skeleton.integrations.tokens import services as token_services

    team = calendar(agenda, "Time")
    url = issue_link(login(agenda.bob), agenda, team)
    assert Client().get("/me/apps/skeleton_calendar/calendar-feed/skt_00000000_" + "A" * 43 + ".ics").status_code == 404
    with override_settings(SKELETON_API_TOKEN_RATE_PER_MINUTE=1):
        assert Client().get(url).status_code == 200
        assert Client().get(url).status_code == 404  # Over the limit: the same answer as a guess.
    later = timezone.now() + timedelta(days=366)
    monkeypatch.setattr(token_services, "timezone", SimpleNamespace(now=lambda: later))
    assert Client().get(url).status_code == 404


def test_the_link_page_needs_reading_the_calendar(agenda, login):
    private = calendar(agenda, "Diretoria", visibility="private")
    bob = login(agenda.bob)
    assert bob.get(link_page(agenda, private)).status_code == 404
    response = bob.post(link_page(agenda, private), {"csrfmiddlewaretoken": bob.cookies["csrftoken"].value})
    assert response.status_code == 404
    # Another organization's page of the same calendar does not exist either.
    other = reverse("skeleton_calendar:calendar-link", kwargs={"organization_id": agenda.b.pk, "sysid": private.pk})
    assert login(agenda.alice).get(other).status_code == 404
    with actor_tenant(agenda.bob, agenda.a), pytest.raises(NotFound):
        FeedTokenService().issue_for_record(
            actor=agenda.bob,
            name="Link",
            scope="calendar.link",
            expires_at=timezone.now() + timedelta(days=30),
            table_key=TABLE,
            record_id=private.pk,
        )


def test_a_link_of_organization_a_stops_when_the_membership_ends(agenda, login):
    from skeleton.access.services import update_membership
    from skeleton.tenancy.models import Membership

    team = calendar(agenda, "Time")
    event(agenda, team, "Reunião do time")
    url = issue_link(login(agenda.alice), agenda, team)
    assert summaries(Client().get(url)) == ["Reunião do time"]
    with actor_tenant(agenda.admin, agenda.a):
        membership = Membership.objects.get(user=agenda.alice)
        update_membership(membership, user=agenda.admin, data={"is_active": False})
    assert Client().get(url).status_code == 404
