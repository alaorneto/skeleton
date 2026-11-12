"""Inbox: list, counter, read/unread/archive and authorized opening (#52)."""

import json

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from skeleton.notifications import registry
from skeleton.notifications.inbox import counter_label, unread_count
from skeleton.notifications.models import NotificationRecipient
from skeleton.notifications.registry import NotificationType, register_notification_type
from skeleton.notifications.services import Recipients, notify
from skeleton.tenancy.context import request_context, system_context
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.service_api import FIXTURE_PASSWORD, build_service_scenario

pytestmark = pytest.mark.usefixtures("skeleton_db")


@pytest.fixture
def scenario(monkeypatch):
    from django.apps import apps

    if not apps.is_installed("examples.notes"):
        pytest.skip("SKELETON_ENABLE_EXAMPLE_APP=true is required")
    from examples.notes.models import Note

    monkeypatch.setattr(registry, "_types", {})
    monkeypatch.setattr(registry, "_duplicates", [])
    register_notification_type(
        NotificationType(
            "notes.mention", "Você foi mencionado", snapshot_fields=("title",), target_table_key="example_notes.note"
        )
    )
    register_notification_type(NotificationType("system.notice", "Aviso"))
    return build_service_scenario(Note, slug="inbox")


def login(user):
    client = Client(enforce_csrf_checks=True)
    client.get(reverse("account_login"))
    response = client.post(
        reverse("account_login"),
        {"login": user.email, "password": FIXTURE_PASSWORD, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
    )
    assert response.status_code == 302
    return client


def post(client, url, data=None):
    return client.post(url, {**(data or {}), "csrfmiddlewaretoken": client.cookies["csrftoken"].value})


def notice(scenario, organization, admin, users):
    with scenario.acting(admin, organization):
        notify("system.notice", recipients=Recipients(users=tuple(user.pk for user in users)))
    run_pending_jobs()


def member_row(scenario, organization):
    with system_context(reason="test:inbox-row", organization_id=organization.pk):
        return NotificationRecipient.objects.get(user=scenario.member)


def test_global_inbox_counter_and_actions_without_javascript(scenario):
    notice(scenario, scenario.a, scenario.admin_a, [scenario.member])
    notice(scenario, scenario.b, scenario.admin_b, [scenario.member])
    client = login(scenario.member)
    page = client.get(reverse("notifications:global"))
    assert page.status_code == 200 and page.content.decode().count("Aviso") >= 2
    assert client.get(reverse("api:me")).status_code == 200
    api = client.get(reverse("notifications:api-inbox")).json()
    assert (api["count"], api["unread"]) == (2, 2)
    row = member_row(scenario, scenario.a)
    url = reverse("notifications:change", args=[scenario.a.pk, row.pk, "read"])
    assert post(client, url, {"from": "global"})["Location"] == reverse("notifications:global")
    assert post(client, url).status_code == 302  # Idempotent.
    assert client.get(reverse("notifications:api-inbox")).json()["unread"] == 1
    assert post(client, reverse("notifications:change", args=[scenario.a.pk, row.pk, "archive"])).status_code == 302
    assert client.get(reverse("notifications:api-inbox")).json()["count"] == 1
    assert post(client, reverse("notifications:read-all", args=[scenario.b.pk])).status_code == 302
    assert client.get(reverse("notifications:api-inbox")).json()["unread"] == 0
    # A POST without the CSRF token is refused, authenticated or not: CSRF runs before the login redirect.
    unread = client.get(reverse("notifications:api-inbox")).json()["unread"]
    assert client.post(reverse("notifications:change", args=[scenario.a.pk, row.pk, "unread"])).status_code == 403
    assert client.get(reverse("notifications:api-inbox")).json()["unread"] == unread
    assert Client(enforce_csrf_checks=True).post(url).status_code == 403


def test_marking_someone_elses_row_is_404(scenario):
    notice(scenario, scenario.a, scenario.admin_a, [scenario.member])
    row = member_row(scenario, scenario.a)
    client = login(scenario.denied)
    assert post(client, reverse("notifications:change", args=[scenario.a.pk, row.pk, "read"])).status_code == 404
    response = client.post(
        reverse("notifications:api-actions", args=[scenario.a.pk]),
        data=json.dumps({"action": "read", "ids": [str(row.pk)]}),
        content_type="application/json",
        HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
    )
    assert response.status_code == 404
    assert member_row(scenario, scenario.a).read_at is None


def test_counter_excludes_inactive_memberships_and_is_capped(scenario):
    from skeleton.tenancy.models import Membership

    notice(scenario, scenario.a, scenario.admin_a, [scenario.member])
    notice(scenario, scenario.b, scenario.admin_b, [scenario.member])
    with request_context(actor=scenario.member, request_id=None, source="test:inbox-count"):
        assert unread_count(scenario.member) == 2
    with system_context(reason="test:inbox-deactivate", organization_id=scenario.b.pk):
        membership = Membership.objects.get(user=scenario.member)
        membership.is_active = False
        membership.save(update_fields={"is_active"})
    with request_context(actor=scenario.member, request_id=None, source="test:inbox-count-after"):
        assert unread_count(scenario.member) == 1
    assert (counter_label(5), counter_label(99), counter_label(100)) == ("5", "99", "99+")


def test_counter_query_count_is_constant(scenario):
    notice(scenario, scenario.a, scenario.admin_a, [scenario.member])
    with request_context(actor=scenario.member, request_id=None, source="test:inbox-queries"):
        with CaptureQueriesContext(connection) as one:
            unread_count(scenario.member)
    for _ in range(3):
        notice(scenario, scenario.b, scenario.admin_b, [scenario.member])
    with request_context(actor=scenario.member, request_id=None, source="test:inbox-queries-more"):
        with CaptureQueriesContext(connection) as many:
            unread_count(scenario.member)
    assert len(one.captured_queries) == len(many.captured_queries)


def test_opening_after_losing_access_shows_a_generic_page(scenario):
    note = scenario.create(
        scenario.a, owner=scenario.admin_a, code="IN-1", title="Secret plan", visibility="organization"
    )
    with scenario.acting(scenario.admin_a, scenario.a):
        notify("notes.mention", recipients=Recipients(users=(scenario.member.pk,)), target=note)
    run_pending_jobs()
    row = member_row(scenario, scenario.a)
    client = login(scenario.member)
    opened = client.get(reverse("notifications:open", args=[scenario.a.pk, row.pk]))
    assert opened.status_code == 302 and str(note.pk) in opened["Location"]
    with system_context(reason="test:inbox-revoke", organization_id=scenario.a.pk):
        from examples.notes.models import Note

        locked = Note.objects.get(pk=note.pk)
        locked.visibility = "private"
        locked.save(update_fields={"visibility"})
    again = client.get(reverse("notifications:open", args=[scenario.a.pk, row.pk]))
    assert again.status_code == 200
    body = again.content.decode()
    assert "Secret plan" not in body and "indisponível" in body
    assert member_row(scenario, scenario.a).read_at is not None
