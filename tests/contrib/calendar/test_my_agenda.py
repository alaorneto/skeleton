"""Minha agenda (#75): every organization in its own context; revocations apply on the next request."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.urls import reverse

from skeleton.contrib.calendar import services
from tools.security_fixtures import actor_tenant

URL = "/me/apps/skeleton_calendar/"


def tomorrow_at_ten():
    day = datetime.now(ZoneInfo("UTC")).date() + timedelta(days=1)
    return datetime(day.year, day.month, day.day, 10, 0, tzinfo=ZoneInfo("UTC"))


def plan(agenda, organization, user, title):
    start = tomorrow_at_ten()
    with actor_tenant(user, organization):
        calendar = services.ensure_personal_calendar(user)
        services.create_event(
            actor=user,
            calendar_id=calendar.pk,
            data={"title": title, "starts_at": start, "ends_at": start + timedelta(hours=1)},
        )


def grant_b(agenda):
    with actor_tenant(agenda.bob, agenda.b):  # Bob administers B.
        services.grant_member_access(actor=agenda.bob)


def test_items_of_every_organization_appear_and_revocation_removes_them(agenda, login):
    grant_b(agenda)
    plan(agenda, agenda.a, agenda.alice, "Reunião em A")
    plan(agenda, agenda.b, agenda.alice, "Reunião em B")
    client = login(agenda.alice)
    assert reverse("skeleton_calendar_me:mine") == URL
    html = client.get(URL).content.decode()
    assert "Reunião em A" in html and "Reunião em B" in html
    assert reverse("skeleton_calendar:event-new", kwargs={"organization_id": agenda.b.pk}) in html
    # Revoke the member's read permission in A: A's items disappear on the next request.
    from skeleton.access.models import Role, TablePermission
    from skeleton.access.policies import table_definition
    from skeleton.contrib.calendar.models import Calendar

    with actor_tenant(agenda.admin, agenda.a):
        member = Role.objects.get(code="member", is_builtin=True)
        TablePermission.objects.get(role=member, table=table_definition(Calendar), action="read").delete()
    html = client.get(URL).content.decode()
    assert "Reunião em A" not in html and "Reunião em B" in html


def test_inactive_membership_and_disabled_app_drop_the_organization(agenda, login):
    from skeleton.access.services import update_membership

    grant_b(agenda)
    plan(agenda, agenda.a, agenda.bob, "Bob em A")
    plan(agenda, agenda.b, agenda.bob, "Bob em B")
    with actor_tenant(agenda.admin, agenda.a):
        update_membership(agenda.bob_membership, user=agenda.admin, data={"is_active": False})
    client = login(agenda.bob)
    html = client.get(URL).content.decode()
    assert "Bob em A" not in html and "Bob em B" in html
    from skeleton.system.applications import application_state, configure_application

    with actor_tenant(agenda.bob, agenda.b):
        state = application_state("skeleton_calendar")
        configure_application(
            "skeleton_calendar",
            user=agenda.bob,
            enabled=False,
            configuration=state["configuration"],
            revision=state["revision"],
        )
    assert "Bob em B" not in client.get(URL).content.decode()


def test_anonymous_is_sent_to_login(agenda):
    from django.test import Client

    response = Client().get(URL)
    assert response.status_code == 302 and "/accounts/login/" in response["Location"]


def test_an_organization_over_the_limit_is_reported_not_fatal(agenda, login, monkeypatch):
    """One organization above the occurrence cap is named apart; the others still list (#291)."""
    from skeleton.contrib.calendar import service_api

    grant_b(agenda)
    for index in range(3):
        plan(agenda, agenda.a, agenda.alice, f"Muitos {index}")
    plan(agenda, agenda.b, agenda.alice, "Reunião em B")
    monkeypatch.setattr(service_api, "MAX_OCCURRENCES_PER_CALL", 2)
    response = login(agenda.alice).get(URL)
    assert response.status_code == 200
    html = response.content.decode()
    assert "Reunião em B" in html and "Muitos 0" not in html
    assert "Eventos demais para listar em:" in html and agenda.a.name in html


# --------------------------------------------------------------------------- all-day and time zones (#312)

DAY = datetime(2027, 5, 10).date()


def _profile_zone(user, zone):
    from skeleton.accounts.models import UserProfile
    from skeleton.tenancy.context import system_context

    with system_context(reason="test:calendar-profile-zone"):
        profile = UserProfile.objects.get(user=user)
        profile.timezone = zone
        profile.save(update_fields={"timezone"})


def _all_day(agenda, title, day):
    with actor_tenant(agenda.alice, agenda.a):
        calendar = services.ensure_personal_calendar(agenda.alice)
        services.create_event(
            actor=agenda.alice,
            calendar_id=calendar.pk,
            data={"title": title, "all_day": True, "start_date": day, "end_date": day + timedelta(days=1)},
        )


def test_all_day_events_show_their_date(agenda, login):
    _all_day(agenda, "Feriado", DAY + timedelta(days=1))
    html = login(agenda.alice).get(URL + f"?date={DAY.isoformat()}").content.decode()
    assert "Feriado" in html and "11/05" in html


@pytest.mark.parametrize("zone,outside", [("Asia/Tokyo", -1), ("America/Sao_Paulo", 14)])
def test_all_day_window_uses_local_days(agenda, login, zone, outside):
    """East of UTC, yesterday's all-day event stays out; west of it, the 15th day's (#312)."""
    _all_day(agenda, "Hoje", DAY)
    _all_day(agenda, "Fora", DAY + timedelta(days=outside))
    _profile_zone(agenda.alice, zone)
    html = login(agenda.alice).get(URL + f"?date={DAY.isoformat()}").content.decode()
    assert "Hoje" in html and "Fora" not in html


def test_times_are_shown_in_the_profile_time_zone(agenda, login):
    noon = datetime(2027, 5, 11, 12, 0, tzinfo=ZoneInfo("UTC"))
    with actor_tenant(agenda.alice, agenda.a):
        calendar = services.ensure_personal_calendar(agenda.alice)
        services.create_event(
            actor=agenda.alice,
            calendar_id=calendar.pk,
            data={"title": "Meio-dia UTC", "starts_at": noon, "ends_at": noon + timedelta(hours=1)},
        )
    _profile_zone(agenda.alice, "America/Sao_Paulo")
    html = login(agenda.alice).get(URL + f"?date={DAY.isoformat()}").content.decode()
    row = html.split("Meio-dia UTC")[0].rsplit("<tr>", 1)[1]
    assert "09:00" in row and "12:00" not in row
