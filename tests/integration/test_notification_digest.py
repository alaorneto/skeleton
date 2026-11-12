"""Daily and weekly email digests, idempotent per window (#55).

Runs on PostgreSQL with the runtime role (one transaction per organization).
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from django.core import mail

from skeleton.notifications.channels.digest import run_digest
from skeleton.notifications.inbox import mark_read
from skeleton.notifications.models import NotificationDelivery, NotificationRecipient
from skeleton.notifications.preferences import set_preference
from skeleton.notifications.registry import NotificationType, register_notification_type
from skeleton.notifications.services import Recipients, notify
from skeleton.tenancy.context import request_context, system_context
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.registries import isolated_notification_types
from skeleton.testing.service_api import build_service_scenario

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]
# 12:00 UTC on a Monday is 09:00 in São Paulo.
MONDAY_NOON = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
TUESDAY_NOON = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


@pytest.fixture
def scenario(monkeypatch, settings):

    from examples.notes.models import Note

    settings.SKELETON_PUBLIC_URL = "https://app.example.test"
    with isolated_notification_types():
        register_notification_type(NotificationType("system.notice", "Aviso", default_channels=("in_app", "email")))
        register_notification_type(NotificationType("system.other", "Outro aviso"))
        scenario = build_service_scenario(Note, slug="digest")
        with system_context(reason="test:profile-timezone"):
            profile = scenario.member.profile
            profile.refresh_from_db()
            profile.timezone = "America/Sao_Paulo"
            profile.save(update_fields={"timezone"})
        yield scenario


def prefer(user, mode, hour=9, type_code="*"):
    with request_context(actor=user, request_id=uuid4(), source="test:preferences"):
        set_preference(user, type_code=type_code, channel="email", mode=mode, digest_hour=hour, expected_revision=None)


def notice(scenario, organization, admin, count=1, code="system.notice"):
    with scenario.acting(admin, organization):
        for _ in range(count):
            notify(code, recipients=Recipients(users=(scenario.member.pk,)))
    run_pending_jobs()


def digest(organization, now):
    with system_context(reason="test:digest", organization_id=organization.pk):
        return run_digest(now=now)


def digests(organization):
    with system_context(reason="test:digest-rows", organization_id=organization.pk):
        return list(NotificationDelivery.objects.filter(channel="digest"))


def test_daily_digest_at_the_local_hour_and_never_twice(scenario):
    prefer(scenario.member, "daily")
    notice(scenario, scenario.a, scenario.admin_a, count=2)
    assert mail.outbox == []  # Digest types send no immediate email.
    assert digest(scenario.a, TUESDAY_NOON.replace(hour=11)) == 0  # 08:00 local: not yet.
    assert digest(scenario.a, TUESDAY_NOON) == 1
    run_pending_jobs()
    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    assert message.subject == "Resumo: 2 notificações não lidas"
    assert message.body.count("- Aviso") == 2 and "https://app.example.test/o/" in message.body
    assert digest(scenario.a, TUESDAY_NOON) == 0  # Same window again: nothing new.
    notice(scenario, scenario.a, scenario.admin_a)
    assert digest(scenario.a, TUESDAY_NOON) == 0  # Same local hour: the window already has its digest.
    run_pending_jobs()
    assert len(mail.outbox) == 1 and len(digests(scenario.a)) == 1


def test_read_items_stay_out_and_empty_digests_are_not_sent(scenario):
    prefer(scenario.member, "daily")
    notice(scenario, scenario.a, scenario.admin_a, count=2)
    with system_context(reason="test:rows", organization_id=scenario.a.pk):
        rows = list(NotificationRecipient.objects.filter(user=scenario.member).order_by("created_at"))
    with scenario.acting(scenario.member, scenario.a):
        mark_read(scenario.member, [rows[0].pk])
    assert digest(scenario.a, TUESDAY_NOON) == 1
    run_pending_jobs()
    assert mail.outbox[0].subject == "Resumo: 1 notificação não lida"
    with scenario.acting(scenario.member, scenario.a):
        mark_read(scenario.member, [rows[1].pk])
    assert digest(scenario.a, TUESDAY_NOON.replace(day=11)) == 0  # Everything read: no digest at all.
    assert len(digests(scenario.a)) == 1


def test_items_read_between_the_digest_and_the_send_skip_an_empty_email(scenario):
    prefer(scenario.member, "daily")
    notice(scenario, scenario.a, scenario.admin_a)
    assert digest(scenario.a, TUESDAY_NOON) == 1
    with system_context(reason="test:rows", organization_id=scenario.a.pk):
        row = NotificationRecipient.objects.get(user=scenario.member)
    with scenario.acting(scenario.member, scenario.a):
        mark_read(scenario.member, [row.pk])
    run_pending_jobs()
    assert mail.outbox == []
    [skipped] = digests(scenario.a)
    assert (skipped.state, skipped.error_code) == ("skipped", "empty_digest")


def test_weekly_digest_goes_out_only_on_mondays_and_per_organization(scenario):
    prefer(scenario.member, "weekly")
    notice(scenario, scenario.a, scenario.admin_a)
    notice(scenario, scenario.b, scenario.admin_b)
    assert digest(scenario.a, TUESDAY_NOON) == 0
    assert digest(scenario.a, MONDAY_NOON) == 1
    assert digest(scenario.b, MONDAY_NOON) == 1
    run_pending_jobs()
    assert len(mail.outbox) == 2  # One digest per organization (accepted in this phase).
    assert len(digests(scenario.a)) == 1 and len(digests(scenario.b)) == 1


def test_types_left_immediate_are_not_in_the_digest(scenario):
    prefer(scenario.member, "daily", type_code="system.other")
    notice(scenario, scenario.a, scenario.admin_a)  # system.notice: immediate email by default.
    notice(scenario, scenario.a, scenario.admin_a, code="system.other")
    assert len(mail.outbox) == 1
    assert digest(scenario.a, TUESDAY_NOON) == 1
    run_pending_jobs()
    assert len(mail.outbox) == 2 and mail.outbox[1].body.count("- Outro aviso") == 1
    assert "- Aviso" not in mail.outbox[1].body
