"""The administrator edits the organization's business calendar on the settings page (#416)."""

import pytest
from django.urls import reverse

from skeleton.tenancy.business_hours import organization_calendar
from skeleton.testing.service_api import FIXTURE_PASSWORD, build_service_scenario
from tests import support

pytestmark = pytest.mark.needs_example_app


@pytest.fixture
def scenario(skeleton_db):
    from examples.notes.models import Note

    return build_service_scenario(Note, slug="calendar-hours")


def save(client, organization, **fields):
    page = reverse("web:organization-settings", args=[organization.pk])
    form = client.get(page).context["form"]
    data = {**{name: "" if form.initial.get(name) is None else form.initial[name] for name in form.fields}, **fields}
    return client.post(page, {name: value for name, value in data.items() if value is not None})


def test_the_administrator_saves_the_calendar_and_it_stays_in_its_organization(scenario):
    admin = support.login(scenario.admin_a, password=FIXTURE_PASSWORD, csrf=False)
    saved = save(
        admin, scenario.a, business_hours="seg 09:00-12:00 13:00-18:00\nsex 08:00-12:00", holidays="2026-12-25"
    )
    assert saved.status_code == 302, saved.context["form"].errors
    shown = admin.get(reverse("web:organization-settings", args=[scenario.a.pk])).content.decode()
    assert "seg 09:00-12:00 13:00-18:00" in shown and "2026-12-25" in shown
    with scenario.acting(scenario.admin_a, scenario.a):
        assert organization_calendar().holidays and 0 in organization_calendar().hours
    with scenario.acting(scenario.admin_b, scenario.b):
        assert organization_calendar() is None  # B has no calendar.
    wrong = save(admin, scenario.a, business_hours="seg 18:00-09:00")
    assert wrong.status_code == 400


def test_a_member_cannot_change_the_calendar(scenario):
    member = support.login(scenario.member, password=FIXTURE_PASSWORD, csrf=False)
    assert member.get(reverse("web:organization-settings", args=[scenario.a.pk])).status_code in (403, 404)
