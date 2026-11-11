"""Calendar pages (#71): validated parameters, never an unreadable event, landmarks, no JavaScript."""

from datetime import UTC, datetime, timedelta

import pytest
from django.urls import reverse

from skeleton.contrib.calendar import services
from skeleton.contrib.calendar.models import Calendar, Event
from tools.security_fixtures import actor_tenant

DAY = "2026-12-14"  # A Monday.
START = datetime(2026, 12, 14, 13, 0, tzinfo=UTC)


def home(organization):
    return reverse("skeleton_calendar:home", kwargs={"organization_id": organization.pk})


@pytest.mark.parametrize("query", ["?view=year", "?view=week&date=14/12/2026", "?date=2026-13-40", "?date=0001-01-01"])
def test_invalid_parameters_answer_400(agenda, login, query):
    client = login(agenda.alice)
    response = client.get(home(agenda.a) + query)
    assert response.status_code == 400 and "Pedido inválido" in response.content.decode()


@pytest.mark.parametrize("view", ["month", "week", "day", "list"])
def test_each_view_renders_without_scripts_and_with_landmarks(agenda, login, view):
    with actor_tenant(agenda.alice, agenda.a):
        calendar = services.ensure_personal_calendar(agenda.alice)
        services.create_event(
            actor=agenda.alice,
            calendar_id=calendar.pk,
            data={"title": "Visível", "starts_at": START, "ends_at": START + timedelta(hours=1)},
        )
    html = login(agenda.alice).get(home(agenda.a) + f"?view={view}&date={DAY}").content.decode()
    assert "Visível" in html
    assert 'aria-label="Navegação da agenda"' in html and 'aria-current="page"' in html
    assert "<h2 " in html and "<time datetime=" in html


def test_unreadable_and_private_events_are_never_rendered(agenda, login):
    with actor_tenant(agenda.alice, agenda.a):
        personal = services.ensure_personal_calendar(agenda.alice)
        services.create_event(
            actor=agenda.alice,
            calendar_id=personal.pk,
            data={"title": "Consulta médica", "starts_at": START, "ends_at": START + timedelta(hours=1)},
        )
        shared = Calendar(
            organization_id=agenda.a.pk, owner=agenda.alice, name="Time", kind="shared", visibility="organization"
        )
        shared.save()
        services.create_event(
            actor=agenda.alice,
            calendar_id=shared.pk,
            data={
                "title": "Entrevista sigilosa",
                "starts_at": START,
                "ends_at": START + timedelta(hours=1),
                "is_private": True,
            },
        )
    html = login(agenda.bob).get(home(agenda.a) + f"?view=week&date={DAY}").content.decode()
    assert "Consulta médica" not in html  # Alice's personal calendar is not readable by Bob.
    assert "Entrevista sigilosa" not in html
    assert '<span class="text-body-subtle">Ocupado</span>' in html  # A private event is a busy block.


def test_event_form_creates_with_a_plain_post(agenda, login):
    client = login(agenda.alice)
    url = reverse("skeleton_calendar:event-new", kwargs={"organization_id": agenda.a.pk})
    with actor_tenant(agenda.alice, agenda.a):
        calendar = services.ensure_personal_calendar(agenda.alice)
    response = client.post(
        url,
        {
            "calendar": str(calendar.pk),
            "title": "Treino",
            "day": DAY,
            "start": "07:00",
            "end": "08:00",
            "repeat": "weekly",
            "count": "4",
            "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
        },
    )
    assert response.status_code == 302
    with actor_tenant(agenda.alice, agenda.a):
        event = Event.objects.get(title="Treino")
    assert event.rrule == "FREQ=WEEKLY;COUNT=4"
    bad = client.post(
        url,
        {
            "calendar": str(calendar.pk),
            "title": "x",
            "day": DAY,
            "repeat": "none",
            "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
        },
    )
    assert bad.status_code == 400  # Neither times nor all-day.
