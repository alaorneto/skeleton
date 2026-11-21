"""Discovery endpoints for native clients (#605): server facts, active apps and the inbox by token."""

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from skeleton.instance import maintenance
from skeleton.instance.maintenance import enter
from skeleton.instance.store import InstanceStore
from skeleton.integrations.tokens.services import issue_personal_token
from skeleton.tenancy.context import request_context, system_context
from tests import oidc_support as oidc
from tools.security_fixtures import build_security_fixture

NOTES = "example_notes"


def token_of(user, scopes=("example_notes.note:read",)):
    with request_context(actor=user, request_id=uuid4(), source="test:tokens"):
        return issue_personal_token(
            actor=user, name="Desktop", scopes=list(scopes), expires_at=timezone.now() + timedelta(days=7)
        ).text


def bearer(text):
    return Client(HTTP_AUTHORIZATION=f"Bearer {text}")


def active_url(organization):
    return reverse("api-application-active", kwargs={"organization_id": organization.pk})


def test_server_is_anonymous_public_and_open_in_maintenance(skeleton_db, settings, tmp_path, subtests):
    from skeleton.versioning import core_version

    url = reverse("api:server")
    with subtests.test("provider off"):
        response = Client().get(url)
        assert response.status_code == 200 and response["Cache-Control"] == "public, max-age=60"
        assert response.json() == {
            "product": "Skeleton",
            "core_version": core_version()["version"],
            "api_versions": ["v1"],
            "oidc_issuer": None,
        }
    with subtests.test("provider on"):
        oidc.enable_provider(settings)
        settings.SKELETON_PRODUCT_NAME = "Acme"
        settings.SKELETON_PUBLIC_URL = "https://app.example.test"
        body = Client().get(url).json()
        assert body["product"] == "Acme" and body["oidc_issuer"] == "https://app.example.test"
        text = json.dumps(body)
        assert set(body) == {"product", "core_version", "api_versions", "oidc_issuer"}
        assert "PRIVATE KEY" not in text and "testserver" not in text
    with subtests.test("maintenance"):
        path = tmp_path / "instance" / "instance.sqlite3"
        settings.SKELETON_MAINTENANCE_STORE = str(path)
        store = InstanceStore(path)
        try:
            enter(store, message="Migração", ends_at=None, pause_jobs=False, source="test")
            maintenance.reader_for(store.path)._cached = None
            assert Client().get(url).status_code == 200
            assert Client().get(reverse("api:organizations")).status_code == 503
            assert Client().post(url).status_code == 503  # Only the read stays open.
        finally:
            maintenance._readers.pop(str(path), None)


@pytest.mark.needs_example_app
def test_active_apps_for_members_with_session_or_token(skeleton_db, subtests):
    from skeleton.system.models import AppInstallation
    from tests import support

    access = build_security_fixture()
    with subtests.test("member with a token"):
        response = bearer(token_of(access.alice)).get(active_url(access.a))
        assert response.status_code == 200, response.content
        (app,) = [row for row in response.json()["results"] if row["label"] == NOTES]
        assert set(app) == {"app_id", "label", "version", "title"}
    with subtests.test("member with a session"):
        session = support.login(access.bob, password=oidc.PASSWORD, csrf=False)
        assert NOTES in {row["label"] for row in session.get(active_url(access.a)).json()["results"]}
    with subtests.test("not a member"):
        response = bearer(token_of(access.outsider)).get(active_url(access.a))
        assert response.status_code == 404 and response.json()["code"] == "not_found"
    with subtests.test("service token of another organization"):
        from skeleton.accounts.services import create_service_account
        from skeleton.integrations.tokens.services import issue_service_token
        from tools.security_fixtures import actor_tenant

        with actor_tenant(access.admin, access.a):
            account = create_service_account(actor=access.admin, name="ERP")
            issued = issue_service_token(
                actor=access.admin,
                account=account,
                name="ERP",
                scopes=["example_notes.note:read"],
                expires_at=timezone.now() + timedelta(days=7),
            )
        assert bearer(issued.text).get(active_url(access.b)).status_code == 404
    with subtests.test("disabled app is not listed"):
        with system_context(reason="test:disable-app", organization_id=access.a.pk):
            record = AppInstallation.objects.get(app_label=NOTES)
            record.enabled = False
            record.save(update_fields=["enabled"])
        response = bearer(token_of(access.alice)).get(active_url(access.a))
        assert response.status_code == 200
        assert NOTES not in {row["label"] for row in response.json()["results"]}


@pytest.mark.needs_example_app
def test_inbox_by_token_reads_and_marks_only_own_rows(skeleton_db):
    from examples.notes.models import Note
    from skeleton.notifications.models import NotificationRecipient
    from skeleton.notifications.registry import NotificationType, register_notification_type
    from skeleton.notifications.services import Recipients, notify
    from skeleton.testing.jobs import run_pending_jobs
    from skeleton.testing.registries import isolated_notification_types
    from skeleton.testing.service_api import build_service_scenario

    with isolated_notification_types():
        register_notification_type(NotificationType("system.notice", "Aviso"))
        scenario = build_service_scenario(Note, slug="native-inbox")
        with scenario.acting(scenario.admin_a, scenario.a):
            notify("system.notice", recipients=Recipients(users=(scenario.member.pk, scenario.denied.pk)))
        run_pending_jobs()
        member = bearer(token_of(scenario.member))
        listing = member.get(reverse("notifications:api-inbox"))
        assert listing.status_code == 200, listing.content
        assert (listing.json()["count"], listing.json()["unread"]) == (1, 1)
        with system_context(reason="test:inbox-rows", organization_id=scenario.a.pk):
            rows = {row.user_id: row for row in NotificationRecipient.objects.all()}
        mine, other = rows[scenario.member.pk], rows[scenario.denied.pk]
        actions = reverse("notifications:api-actions", kwargs={"organization_id": scenario.a.pk})
        # Another person's row looks missing: the whole request is refused, nothing changes.
        body = json.dumps({"action": "read", "ids": [str(mine.pk), str(other.pk)]})
        assert member.post(actions, data=body, content_type="application/json").status_code == 404
        body = json.dumps({"action": "read", "ids": [str(mine.pk)]})
        marked = member.post(actions, data=body, content_type="application/json")
        assert marked.status_code == 200, marked.content
        assert marked.json() == {"changed": 1, "unread": 0}
        with system_context(reason="test:inbox-check", organization_id=scenario.a.pk):
            assert NotificationRecipient.objects.get(pk=other.pk).read_at is None
