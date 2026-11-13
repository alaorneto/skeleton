"""Retention of notification rows, deliveries and orphan notifications (#57).

Runs on PostgreSQL with the runtime role (one organization per run).
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from skeleton.notifications.inbox import unread_count
from skeleton.notifications.models import Notification, NotificationDelivery, NotificationRecipient
from skeleton.notifications.registry import NotificationType, register_notification_type
from skeleton.notifications.retention import purge_expired_notifications, windows
from skeleton.notifications.services import Recipients, notify
from skeleton.tenancy.context import request_context, system_context
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.registries import isolated_notification_types
from skeleton.testing.service_api import build_service_scenario

pytestmark = pytest.mark.usefixtures("skeleton_db")


@pytest.fixture
def scenario(monkeypatch):

    from examples.notes.models import Note

    with isolated_notification_types():
        register_notification_type(NotificationType("system.notice", "Aviso", default_channels=("in_app", "email")))
        yield build_service_scenario(Note, slug="retention")


def notices(scenario, organization, admin, count):
    with scenario.acting(admin, organization):
        for _ in range(count):
            notify("system.notice", recipients=Recipients(users=(scenario.member.pk,)))
    run_pending_jobs()


def rows(organization):
    with system_context(reason="test:retention-rows", organization_id=organization.pk):
        return list(NotificationRecipient.objects.order_by("created_at", "sysid"))


def age(organization, model, pks, **values):
    with system_context(reason="test:retention-age", organization_id=organization.pk):
        model.objects.filter(pk__in=pks).update(**values)


def purge(organization, now=None):
    with system_context(reason="test:retention", organization_id=organization.pk):
        return purge_expired_notifications(now=now)


def counter(scenario):
    from uuid import uuid4

    with request_context(actor=scenario.member, request_id=uuid4(), source="test:counter"):
        return unread_count(scenario.member)


def test_windows_follow_the_settings(settings):
    settings.SKELETON_NOTIFICATION_READ_RETENTION_DAYS = 7
    settings.SKELETON_NOTIFICATION_UNREAD_RETENTION_DAYS = 14
    settings.SKELETON_NOTIFICATION_DELIVERY_RETENTION_DAYS = 3
    assert windows() == {"read": timedelta(days=7), "unread": timedelta(days=14), "deliveries": timedelta(days=3)}


@pytest.mark.needs_example_app
def test_only_expired_rows_go_and_the_counter_stays_right(scenario):
    notices(scenario, scenario.a, scenario.admin_a, count=5)
    notices(scenario, scenario.b, scenario.admin_b, count=1)
    old_read, recent_read, old_unread, recent_unread, old_archived = rows(scenario.a)
    now = timezone.now()
    age(scenario.a, NotificationRecipient, [old_read.pk], read_at=now - timedelta(days=91))
    age(scenario.a, NotificationRecipient, [recent_read.pk], read_at=now - timedelta(days=89))
    age(scenario.a, NotificationRecipient, [old_unread.pk], updated_at=now - timedelta(days=181))
    age(scenario.a, NotificationRecipient, [recent_unread.pk], updated_at=now - timedelta(days=179))
    age(scenario.a, NotificationRecipient, [old_archived.pk], archived_at=now - timedelta(days=95))
    before = counter(scenario)
    result = purge(scenario.a)
    assert result["rows"] == 3
    assert {row.pk for row in rows(scenario.a)} == {recent_read.pk, recent_unread.pk}
    # Removed: one unread (old_unread). Kept unread: recent_unread in A and the row in B.
    assert counter(scenario) == before - 1 == 2
    assert len(rows(scenario.b)) == 1  # Another organization is never touched.


@pytest.mark.needs_example_app
def test_finished_and_abandoned_deliveries_expire_and_recent_open_ones_stay(scenario):
    """An open delivery past the window was abandoned by a dead job: it goes too (#277)."""
    notices(scenario, scenario.a, scenario.admin_a, count=2)
    with system_context(reason="test:deliveries", organization_id=scenario.a.pk):
        sent, other = NotificationDelivery.objects.order_by("created_at", "sysid")
    age(scenario.a, NotificationDelivery, [other.pk], state="retrying")
    assert purge(scenario.a, now=timezone.now() + timedelta(days=29))["deliveries"] == 0
    assert purge(scenario.a, now=timezone.now() + timedelta(days=31))["deliveries"] == 2
    with system_context(reason="test:deliveries", organization_id=scenario.a.pk):
        assert not NotificationDelivery.objects.exists()


@pytest.mark.needs_example_app
def test_orphan_notifications_go_only_after_their_rows_and_deliveries(scenario):
    notices(scenario, scenario.a, scenario.admin_a, count=1)
    [row] = rows(scenario.a)
    now = timezone.now()
    assert purge(scenario.a, now=now + timedelta(days=31)) == {"deliveries": 1, "rows": 0, "notifications": 0}
    age(scenario.a, NotificationRecipient, [row.pk], read_at=now)
    assert purge(scenario.a, now=now + timedelta(days=95)) == {"deliveries": 0, "rows": 1, "notifications": 0}
    assert purge(scenario.a, now=now + timedelta(days=181)) == {"deliveries": 0, "rows": 0, "notifications": 1}
    with system_context(reason="test:notifications", organization_id=scenario.a.pk):
        assert not Notification.objects.exists()


@pytest.mark.needs_example_app
def test_notifications_never_fanned_out_are_purged_after_the_unread_window(scenario):
    """A fan-out job that died leaves fanned_out_at empty; retention still removes the row (#277)."""
    with scenario.acting(scenario.admin_a, scenario.a):
        notify("system.notice", recipients=Recipients(users=(scenario.member.pk,)))
    # The fan-out job is never run: no inbox row, no delivery.
    with system_context(reason="test:unfanned", organization_id=scenario.a.pk):
        [notification] = Notification.objects.all()
        assert notification.fanned_out_at is None
    assert purge(scenario.a, now=timezone.now() + timedelta(days=179))["notifications"] == 0
    assert purge(scenario.a, now=timezone.now() + timedelta(days=181))["notifications"] == 1
    with system_context(reason="test:unfanned-gone", organization_id=scenario.a.pk):
        assert not Notification.objects.exists()
