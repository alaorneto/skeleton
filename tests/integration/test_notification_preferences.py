"""Notification preferences: page and session API with CAS (#53)."""

import json

import pytest
from django.test import Client
from django.urls import reverse

from skeleton.notifications import registry
from skeleton.notifications.models import UserNotificationPreference
from skeleton.notifications.preferences import effective
from skeleton.notifications.registry import NotificationType, register_notification_type
from skeleton.testing.service_api import FIXTURE_PASSWORD, build_service_scenario

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]


@pytest.fixture
def scenario(monkeypatch):

    from examples.notes.models import Note

    monkeypatch.setattr(registry, "_types", {})
    monkeypatch.setattr(registry, "_duplicates", [])
    register_notification_type(
        NotificationType("tasks.assigned", "Tarefa atribuída", default_channels=("in_app", "email"))
    )
    register_notification_type(NotificationType("security.alert", "Alerta de segurança", mandatory=True))
    return build_service_scenario(Note, slug="preferences")


def login(user):
    client = Client(enforce_csrf_checks=True)
    client.get(reverse("account_login"))
    response = client.post(
        reverse("account_login"),
        {"login": user.email, "password": FIXTURE_PASSWORD, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
    )
    assert response.status_code == 302
    return client


def csrf(client):
    return client.cookies["csrftoken"].value


def put(client, type_code, channel, data, revision=None):
    headers = {"X-CSRFToken": csrf(client)}
    if revision is not None:
        headers["If-Match"] = f'"{revision}"'
    return client.put(
        reverse("notifications:api-preference", args=[type_code, channel]),
        json.dumps(data),
        content_type="application/json",
        headers=headers,
    )


def rows(user):
    return list(UserNotificationPreference.objects.filter(user=user).order_by("type_code", "channel"))


def test_without_rows_the_type_defaults_apply(scenario):
    client = login(scenario.member)
    listing = client.get(reverse("notifications:api-preferences")).json()["results"]
    by_key = {(item["type_code"], item["channel"]): item for item in listing}
    assert by_key[("tasks.assigned", "email")]["mode"] == "immediate"
    assert by_key[("tasks.assigned", "push")]["mode"] == "off"
    assert by_key[("tasks.assigned", "email")]["source"] == "type"
    assert by_key[("security.alert", "in_app")]["source"] == "mandatory"
    assert by_key[("*", "in_app")]["revision"] is None
    assert rows(scenario.member) == []


def test_api_creates_and_changes_own_row_with_cas(scenario):
    client = login(scenario.member)
    created = put(client, "tasks.assigned", "email", {"mode": "daily", "digest_hour": 7})
    assert created.status_code == 200 and created["ETag"] == '"1"'
    assert (created.json()["mode"], created.json()["digest_hour"], created.json()["source"]) == ("daily", 7, "own")
    assert put(client, "tasks.assigned", "email", {"mode": "off"}).status_code == 428
    stale = put(client, "tasks.assigned", "email", {"mode": "off"}, revision=9)
    assert (stale.status_code, stale.json()["code"]) == (412, "revision_conflict")
    changed = put(client, "tasks.assigned", "email", {"mode": "off"}, revision=1)
    assert changed.status_code == 200 and changed["ETag"] == '"2"'
    assert effective(scenario.member, "tasks.assigned", "email").mode == "off"
    other = login(scenario.denied)
    assert other.get(reverse("notifications:api-preference", args=["tasks.assigned", "email"])).json()["mode"] == (
        "immediate"
    )
    gone = client.delete(
        reverse("notifications:api-preference", args=["tasks.assigned", "email"]),
        headers={"X-CSRFToken": csrf(client), "If-Match": '"2"'},
    )
    assert gone.status_code == 204
    assert effective(scenario.member, "tasks.assigned", "email").mode == "immediate"


def test_invalid_and_mandatory_changes_are_refused(scenario):
    client = login(scenario.member)
    mandatory = put(client, "security.alert", "in_app", {"mode": "off"})
    assert (mandatory.status_code, mandatory.json()["code"]) == (400, "validation_error")
    assert put(client, "tasks.assigned", "push", {"mode": "daily"}).status_code == 400
    assert put(client, "tasks.assigned", "email", {"mode": "daily", "digest_hour": 24}).status_code == 400
    assert put(client, "tasks.assigned", "email", {"mode": "off", "user": str(scenario.admin_a.pk)}).status_code == 400
    assert put(client, "tasks.unknown", "email", {"mode": "off"}).status_code == 404
    assert put(client, "tasks.assigned", "fax", {"mode": "off"}).status_code == 404
    assert put(client, "tasks.assigned", "email", {"mode": "off"}, revision=1).status_code == 412
    assert rows(scenario.member) == []
    assert Client().get(reverse("notifications:api-preferences")).status_code == 403


def test_page_saves_without_javascript_and_detects_stale_forms(scenario):
    client = login(scenario.member)
    page = client.get(reverse("notifications:preferences"))
    assert page.status_code == 200
    html = page.content.decode()
    assert "Tarefa atribuída" in html and 'name="mode:security.alert:in_app" disabled' in html
    form = {
        "csrfmiddlewaretoken": csrf(client),
        "mode:tasks.assigned:email": "weekly",
        "hour:tasks.assigned:email": "6",
        "revision:tasks.assigned:email": "",
        "mode:*:push": "off",
    }
    assert client.post(reverse("notifications:preferences"), form).status_code == 302
    saved = rows(scenario.member)
    assert [(row.type_code, row.channel, row.mode, row.digest_hour) for row in saved] == [
        ("tasks.assigned", "email", "weekly", 6)
    ]
    stale = client.post(reverse("notifications:preferences"), {**form, "mode:tasks.assigned:email": "off"})
    assert stale.status_code == 409 and rows(scenario.member)[0].mode == "weekly"
    forced = client.post(
        reverse("notifications:preferences"), {"csrfmiddlewaretoken": csrf(client), "mode:security.alert:in_app": "off"}
    )
    assert forced.status_code == 400
    assert client.post(reverse("notifications:preferences"), {**form, "user": "x"}).status_code == 400
    without_csrf = {key: value for key, value in form.items() if key != "csrfmiddlewaretoken"}
    assert client.post(reverse("notifications:preferences"), without_csrf).status_code == 403
