"""Notifications to external people (#535): only by email, through jobs, with the portal link.

Runs on PostgreSQL with the runtime role.
"""

import pytest
from django.core import mail
from django.core.exceptions import ValidationError

from skeleton.notifications.models import NotificationDelivery, NotificationRecipient
from skeleton.notifications.registry import NotificationType, notification_problems, register_notification_type
from skeleton.notifications.services import Recipients, notify
from skeleton.tenancy.context import system_context
from skeleton.testing.external import external_scenario
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.registries import isolated_notification_types

pytestmark = pytest.mark.usefixtures("skeleton_db")
EXTERNAL = "acme_portal.document_released"
MEMBERS = "acme_portal.internal"


@pytest.fixture
def scenario(settings):
    settings.SKELETON_PUBLIC_URL = "https://app.example.test"
    with isolated_notification_types():
        register_notification_type(
            NotificationType(
                EXTERNAL,
                "Documento disponível",
                payload_fields=("title",),
                default_channels=("email",),
                audience="external",
            )
        )
        register_notification_type(NotificationType(MEMBERS, "Interno", default_channels=("in_app", "email")))
        yield external_scenario("ext-notify")


def send(scenario, type_code, users, *, key=None):
    with system_context(reason="test:external-notify", organization_id=scenario.a.pk):
        return notify(
            type_code,
            recipients=Recipients(users=tuple(user.pk for user in users)),
            payload={"title": "Laudo"} if type_code == EXTERNAL else {},
            dedupe_key=key,
        )


def rows(organization, model):
    with system_context(reason="test:external-notify-read", organization_id=organization.pk):
        return list(model.objects.all())


def test_an_external_type_emails_only_external_people_with_the_portal_link(scenario):
    notification = send(scenario, EXTERNAL, [scenario.external], key="doc-1")
    assert send(scenario, EXTERNAL, [scenario.external], key="doc-1").pk == notification.pk  # Deduplicated.
    run_pending_jobs()
    assert rows(scenario.a, NotificationRecipient) == []  # No inbox row for an external person.
    [delivery] = rows(scenario.a, NotificationDelivery)
    assert (delivery.channel, delivery.state, delivery.user_id) == ("email", "sent", scenario.external.pk)
    [message] = mail.outbox
    assert message.to == [scenario.external.email]
    assert f"/portal/{scenario.a.pk}/" in message.body and "preferências" not in message.body
    assert "List-Unsubscribe" not in message.extra_headers


def test_recipients_follow_the_audience(scenario):
    with pytest.raises(ValidationError):
        send(scenario, MEMBERS, [scenario.external])  # A members' type refuses a non-member, as before.
    with pytest.raises(ValidationError):
        send(scenario, EXTERNAL, [scenario.member])  # A member without external access.
    with system_context(reason="test:external-notify", organization_id=scenario.a.pk), pytest.raises(ValidationError):
        notify(EXTERNAL, recipients=Recipients(organization=True), payload={"title": "x"})


def test_a_revoked_access_stops_the_email(scenario):
    from skeleton.tenancy.models import ExternalAccess

    send(scenario, EXTERNAL, [scenario.external])
    run_pending_jobs(limit=1)  # The fan-out: the email delivery is queued.
    assert [row.state for row in rows(scenario.a, NotificationDelivery)] == ["pending"]
    with system_context(reason="test:external-revoke", organization_id=scenario.a.pk):
        access = ExternalAccess.objects.get(user=scenario.external)
        access.is_active = False
        access.save(update_fields={"is_active"})
    run_pending_jobs()
    assert mail.outbox == []
    [delivery] = rows(scenario.a, NotificationDelivery)
    assert (delivery.state, delivery.error_code) == ("skipped", "recipient_inactive")


def test_the_registry_refuses_an_external_type_with_a_target_or_another_channel():
    with isolated_notification_types():
        register_notification_type(
            NotificationType("acme_portal.bad", "Ruim", default_channels=("in_app", "email"), audience="external")
        )
        register_notification_type(NotificationType("acme_portal.odd", "Estranho", audience="everyone"))
        problems = notification_problems()
    assert any("acme_portal.bad" in problem for problem in problems)
    assert any("acme_portal.odd" in problem for problem in problems)
