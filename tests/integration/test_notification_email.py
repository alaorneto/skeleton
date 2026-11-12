"""Immediate notification email through the job queue, with retries (#54).

Runs on PostgreSQL with the runtime role (RLS of the delivery rows).
"""

from datetime import timedelta
from uuid import uuid4

import pytest
from django.core import mail
from django.core.mail.backends.locmem import EmailBackend
from django.db import transaction
from django.test import override_settings

from skeleton.notifications import registry
from skeleton.notifications.channels import delivery as delivery_module
from skeleton.notifications.models import NotificationDelivery
from skeleton.notifications.preferences import set_preference
from skeleton.notifications.registry import NotificationType, register_notification_type
from skeleton.notifications.services import Recipients, notify
from skeleton.tenancy.context import request_context, system_context
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.service_api import build_service_scenario

pytestmark = pytest.mark.usefixtures("skeleton_db")


@pytest.fixture
def scenario(monkeypatch, settings):
    from django.apps import apps

    if not apps.is_installed("examples.notes"):
        pytest.skip("SKELETON_ENABLE_EXAMPLE_APP=true is required")
    from examples.notes.models import Note

    settings.SKELETON_PUBLIC_URL = "https://app.example.test"
    monkeypatch.setattr(registry, "_types", {})
    monkeypatch.setattr(registry, "_duplicates", [])
    monkeypatch.setattr(delivery_module, "RETRY_SCHEDULE", (timedelta(0),) * 4)
    register_notification_type(
        NotificationType(
            "notes.mention",
            "Você foi mencionado",
            snapshot_fields=("title",),
            target_table_key="example_notes.note",
            default_channels=("in_app", "email"),
        )
    )
    register_notification_type(NotificationType("system.notice", "Aviso"))
    register_notification_type(NotificationType("system.alert", "Alerta", default_channels=("in_app", "email")))
    return build_service_scenario(Note, slug="email")


def mention(scenario, note, users):
    with scenario.acting(scenario.admin_a, scenario.a):
        notify(
            "notes.mention",
            recipients=Recipients(users=tuple(user.pk for user in users)),
            target=note,
        )


def deliveries(organization):
    with system_context(reason="test:notification-email", organization_id=organization.pk):
        return list(NotificationDelivery.objects.order_by("created_at"))


def set_language(user, language):
    with system_context(reason="test:profile-language"):
        profile = user.profile
        profile.refresh_from_db()
        profile.language = language
        profile.save(update_fields={"language"})


# The core ships pt-BR only (#170); a project that adds English gets the .en templates.
@override_settings(LANGUAGES=[("pt-br", "Português"), ("en", "English")])
def test_email_goes_out_in_the_language_of_the_person(scenario):
    note = scenario.create(scenario.a, owner=scenario.admin_a, code="E-1", title="Plano de voo")
    set_language(scenario.member, "en")
    mention(scenario, note, [scenario.member])
    assert mail.outbox == []  # Nothing leaves inside the notify() transaction.
    run_pending_jobs()
    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    assert message.to == [scenario.member.email]
    assert message.subject == "Você foi mencionado"
    assert "Open: https://app.example.test/o/" in message.body and "Plano de voo" in message.body
    assert message.extra_headers["List-Unsubscribe"] == "<https://app.example.test/profile/notifications/>"
    [sent] = deliveries(scenario.a)
    assert (sent.channel, sent.state, sent.attempts) == ("email", "sent", 1)
    run_pending_jobs()
    assert len(mail.outbox) == 1  # The sent delivery never goes out twice.


def test_default_language_is_portuguese_and_types_without_email_send_nothing(scenario):
    note = scenario.create(scenario.a, owner=scenario.admin_a, code="E-2", title="Relatório")
    mention(scenario, note, [scenario.member])
    with scenario.acting(scenario.admin_a, scenario.a):
        notify("system.notice", recipients=Recipients(users=(scenario.member.pk,)))
    run_pending_jobs()
    assert len(mail.outbox) == 1 and "Abrir: https://app.example.test/o/" in mail.outbox[0].body


def test_people_who_opted_out_receive_nothing(scenario):
    note = scenario.create(scenario.a, owner=scenario.admin_a, code="E-3", title="Privado")
    with request_context(actor=scenario.member, request_id=uuid4(), source="test:preferences"):
        set_preference(scenario.member, type_code="*", channel="email", mode="off", expected_revision=None)
    mention(scenario, note, [scenario.member])
    run_pending_jobs()
    assert mail.outbox == [] and deliveries(scenario.a) == []


def test_opt_out_or_deactivation_before_the_send_skips_the_delivery(scenario):
    from skeleton.accounts.models import User

    with scenario.acting(scenario.admin_a, scenario.a):
        notify("system.alert", recipients=Recipients(users=(scenario.member.pk, scenario.denied.pk)))
    run_pending_jobs(limit=1)  # Only the fan-out: both deliveries are queued.
    assert len(deliveries(scenario.a)) == 2
    with request_context(actor=scenario.member, request_id=uuid4(), source="test:preferences"):
        set_preference(scenario.member, type_code="system.alert", channel="email", mode="off", expected_revision=None)
    with system_context(reason="test:deactivate"):
        user = User.objects.get(pk=scenario.denied.pk)
        user.is_active = False
        user.save(update_fields={"is_active"})
    run_pending_jobs()
    assert mail.outbox == []
    states = {(row.user_id, row.state, row.error_code) for row in deliveries(scenario.a)}
    assert states == {
        (scenario.member.pk, "skipped", "opted_out"),
        (scenario.denied.pk, "skipped", "recipient_inactive"),
    }


def test_smtp_failure_is_retried_and_then_dead(scenario, monkeypatch):
    calls = []

    def refuse(self, messages):
        calls.append(len(messages))
        raise ConnectionRefusedError("smtp down")

    monkeypatch.setattr(EmailBackend, "send_messages", refuse)
    note = scenario.create(scenario.a, owner=scenario.admin_a, code="E-5", title="Falha")
    mention(scenario, note, [scenario.member])
    run_pending_jobs()
    [dead] = deliveries(scenario.a)
    assert (dead.state, dead.attempts, dead.error_code) == ("dead", 5, "send_ConnectionRefusedError")
    assert len(calls) == 5 and mail.outbox == []


def test_rolled_back_notification_sends_nothing(scenario):
    note = scenario.create(scenario.a, owner=scenario.admin_a, code="E-6", title="Desfeito")
    with pytest.raises(RuntimeError), transaction.atomic():
        mention(scenario, note, [scenario.member])
        raise RuntimeError("rollback")
    run_pending_jobs()
    assert mail.outbox == [] and deliveries(scenario.a) == []


def test_deliveries_of_another_organization_stay_out_of_sight(scenario):
    note = scenario.create(scenario.a, owner=scenario.admin_a, code="E-7", title="Só A")
    mention(scenario, note, [scenario.member])
    run_pending_jobs()
    assert len(deliveries(scenario.a)) == 1
    assert deliveries(scenario.b) == []
