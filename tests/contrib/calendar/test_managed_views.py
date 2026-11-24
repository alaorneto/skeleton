"""Managed events in the pages and the feed (#540): read-only mark, source link, nothing of the source in ICS."""

import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from django.test import Client
from django.urls import reverse

from skeleton.contrib.calendar.models import Calendar
from skeleton.contrib.calendar.service_api import CalendarService
from skeleton.tenancy.context import request_context, system_context
from skeleton.testing.applications import activate_app, open_app
from tools.security_fixtures import actor_tenant

pytestmark = pytest.mark.needs_example_app
SOURCE = "example_notes"
PATH = f"apps/{SOURCE}/appointments/42/"
START = (datetime.now(UTC) + timedelta(days=2)).replace(hour=13, minute=0, second=0, microsecond=0)
DAY = START.date().isoformat()


@pytest.fixture
def managed(agenda):
    open_app(SOURCE)
    activate_app(SOURCE, agenda.a.pk)
    with actor_tenant(agenda.alice, agenda.a):
        calendar = Calendar(
            organization_id=agenda.a.pk,
            owner=agenda.alice,
            name="Atendimentos",
            kind="shared",
            visibility="organization",
        )
        calendar.save()
    with system_context(reason="test:managed-views", organization_id=agenda.a.pk):
        event = CalendarService().put_managed_event(
            actor=None,
            source_app=SOURCE,
            source_key="appointment-42",
            calendar_id=calendar.pk,
            data={
                "title": "Atendimento",
                "starts_at": START,
                "ends_at": START + timedelta(hours=1),
                "is_private": False,
            },
            attendee_user_ids=(agenda.bob.pk,),
            source_path=PATH,
        )
    return calendar, event


def home(organization):
    return reverse("skeleton_calendar:home", kwargs={"organization_id": organization.pk})


def test_the_page_marks_a_managed_event_and_links_to_the_source(agenda, login, managed):
    html = login(agenda.alice).get(home(agenda.a) + f"?view=week&date={DAY}").content.decode()
    origin = reverse("web:dashboard", kwargs={"organization_id": agenda.a.pk}) + PATH
    assert "Gerenciado por" in html and f'href="{origin}"' in html and "abrir na origem" in html
    assert not re.search(r"<script(?![^>]*src=)", html)  # No inline script, as everywhere.


def test_with_the_source_app_disabled_the_link_disappears(agenda, login, managed):
    from skeleton.testing.service_api import _set_enabled

    with actor_tenant(agenda.admin, agenda.a) as admin:
        _set_enabled(SOURCE, admin or agenda.admin, enabled=False)
    html = login(agenda.alice).get(home(agenda.a) + f"?view=week&date={DAY}").content.decode()
    assert "origem indisponível" in html and PATH not in html


def test_a_forged_cancel_of_a_managed_event_answers_409(agenda, login, managed):
    _, event = managed
    client = login(agenda.alice)
    client.get(home(agenda.a))
    response = client.post(
        reverse("skeleton_calendar:event-cancel", kwargs={"organization_id": agenda.a.pk, "sysid": event.sysid}),
        {"revision": event.revision, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
    )
    assert response.status_code == 409


def test_ics_and_feed_carry_nothing_of_the_source(agenda, login, managed):
    from django.utils import timezone

    from skeleton.service_api.tokens import FeedTokenService

    calendar, _ = managed
    download = login(agenda.alice).get(
        reverse("skeleton_calendar:calendar-ics", kwargs={"organization_id": agenda.a.pk, "sysid": calendar.pk})
    )
    assert download.status_code == 200 and b"Atendimento" in download.content
    assert PATH.encode() not in download.content and SOURCE.encode() not in download.content
    with request_context(actor=agenda.bob, request_id=uuid4(), source="test:feed"):
        issued = FeedTokenService().issue(
            actor=agenda.bob, name="Feed", scope="calendar.feed", expires_at=timezone.now() + timedelta(days=30)
        )
    feed = Client().get(f"/me/apps/skeleton_calendar/feed/{issued.text}.ics").content
    assert b"Atendimento" in feed and PATH.encode() not in feed and SOURCE.encode() not in feed


def test_my_agenda_lists_the_managed_event_of_the_attendee(agenda, login, managed):
    html = login(agenda.bob).get("/me/apps/skeleton_calendar/").content.decode()
    assert "Atendimento" in html and "Gerenciado por outra app" in html
