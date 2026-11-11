"""Calendars (#68): one default personal calendar, private lock, default deny and isolation."""

import json

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse

from skeleton.contrib.calendar import services
from skeleton.contrib.calendar.models import Calendar
from tools.security_fixtures import actor_tenant

TABLE = "skeleton_calendar.calendar"


def test_one_default_personal_calendar_per_member(agenda, monkeypatch):
    with actor_tenant(agenda.alice, agenda.a):
        first = services.ensure_personal_calendar(agenda.alice)
        assert services.ensure_personal_calendar(agenda.alice).pk == first.pk
        assert (first.kind, first.is_default, first.visibility) == ("personal", True, "private")
        # The database refuses a second default, whatever the code path.
        duplicate = Calendar(
            organization_id=agenda.a.pk, owner=agenda.alice, kind="personal", is_default=True, name="Outro"
        )
        with pytest.raises((IntegrityError, ValidationError)), transaction.atomic():
            duplicate.save()
        # A concurrent first visit that lost the race reads the winner instead of failing.
        original = Calendar.objects.filter

        class Missing:
            def first(self):
                return None

        calls = {"n": 0}

        def racing_filter(*args, **kwargs):
            calls["n"] += 1
            return Missing() if calls["n"] == 1 else original(*args, **kwargs)

        monkeypatch.setattr(Calendar.objects, "filter", racing_filter)
        assert services.ensure_personal_calendar(agenda.alice).pk == first.pk
        monkeypatch.undo()
        assert Calendar.objects.filter(owner=agenda.alice, kind="personal", is_default=True).count() == 1


def test_personal_visibility_is_locked(agenda, login):
    from skeleton.service_api.access import AccessService

    with actor_tenant(agenda.admin, agenda.a):
        AccessService().grant_member_table_access(actor=agenda.admin, table_key=TABLE, actions=("manage_access",))
    with actor_tenant(agenda.alice, agenda.a):
        personal = services.ensure_personal_calendar(agenda.alice)
        personal.visibility = "organization"
        with pytest.raises(ValidationError):
            personal.save()
        personal = Calendar.objects.get(pk=personal.pk)  # The refused save changed only the copy in memory.
    client = login(agenda.alice)
    url = reverse(
        "api:record-access", kwargs={"organization_id": agenda.a.pk, "table_key": TABLE, "sysid": personal.pk}
    )
    response = client.patch(
        url,
        data=json.dumps({"visibility": "organization"}),
        content_type="application/json",
        HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
        HTTP_IF_MATCH=f'"{personal.revision}"',
    )
    assert response.status_code == 400
    with actor_tenant(agenda.alice, agenda.a):
        personal = Calendar.objects.get(pk=personal.pk)
        assert personal.visibility == "private"
    delete = client.delete(
        reverse("api:record", kwargs={"organization_id": agenda.a.pk, "table_key": TABLE, "sysid": personal.pk}),
        HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
        HTTP_IF_MATCH=f'"{personal.revision}"',
    )
    assert delete.status_code == 400, delete.content  # The default personal calendar is never deleted.


def test_gateway_creates_shared_calendars(agenda, login):
    client = login(agenda.alice)
    response = client.post(
        reverse("api:records", kwargs={"organization_id": agenda.a.pk, "table_key": TABLE}),
        data=json.dumps({"name": "Equipe", "timezone": "America/Sao_Paulo"}),
        content_type="application/json",
        HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
    )
    assert response.status_code == 201, response.content
    assert response.json()["kind"] == "shared" and response.json()["is_default"] is False
    bad = client.post(
        reverse("api:records", kwargs={"organization_id": agenda.a.pk, "table_key": TABLE}),
        data=json.dumps({"name": "X", "timezone": "Mars/Base"}),
        content_type="application/json",
        HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
    )
    assert bad.status_code == 400


def test_member_without_table_permission_gets_404_and_b_is_isolated(agenda, login):
    home_b = reverse("skeleton_calendar:home", kwargs={"organization_id": agenda.b.pk})
    alice = login(agenda.alice)
    assert alice.get(home_b).status_code == 404  # Alice is a member of B without calendar permission.
    home_a = reverse("skeleton_calendar:home", kwargs={"organization_id": agenda.a.pk})
    assert alice.get(home_a).status_code == 200
    with actor_tenant(agenda.alice, agenda.a):
        personal = services.ensure_personal_calendar(agenda.alice)
    bob = login(agenda.bob)
    foreign = reverse("api:record", kwargs={"organization_id": agenda.b.pk, "table_key": TABLE, "sysid": personal.pk})
    assert bob.get(foreign).status_code == 404
    with actor_tenant(agenda.bob, agenda.b):
        assert not Calendar.objects.filter(pk=personal.pk).exists()


def test_member_access_is_an_admin_action(agenda, login):
    url = reverse("skeleton_calendar:member-access", kwargs={"organization_id": agenda.a.pk})
    alice = login(agenda.alice)
    assert alice.post(url, {"csrfmiddlewaretoken": alice.cookies["csrftoken"].value}).status_code == 404
    admin = login(agenda.admin)
    assert admin.post(url, {"csrfmiddlewaretoken": admin.cookies["csrftoken"].value}).status_code == 302
    with actor_tenant(agenda.admin, agenda.a):
        assert services.grant_member_access(actor=agenda.admin) == ()  # Already granted: no change.
