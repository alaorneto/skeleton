"""The notices the platform sends by itself (#788): the watcher and the erasure request."""

from datetime import timedelta
from uuid import uuid4

import pytest
from django.utils import timezone

from skeleton.notifications import platform_alerts as alerts
from skeleton.notifications.models import PlatformNotification, PlatformNotificationRecipient
from skeleton.tenancy.context import request_context, system_context
from skeleton.testing.jobs import run_pending_jobs
from tests.integration.appearance_support import member, platform_admin

pytestmark = pytest.mark.usefixtures("skeleton_db")


@pytest.fixture
def admin():
    return platform_admin("platform-alerts-admin@example.test")


def notices(type_code):
    with system_context(reason="test:platform-alerts-read"):
        return list(PlatformNotification.objects.filter(type_code=type_code).values_list("payload", flat=True))


def watch(now=None):
    with system_context(reason="test:platform-watch"):
        failed = alerts.platform_watch(now=now)
    return failed


def dead_jobs(count):
    from skeleton.jobs.models import Job
    from skeleton.jobs.services import enqueue

    with system_context(reason="test:platform-alerts-dead"):
        ids = [enqueue(alerts.WATCH_JOB, idempotency_key=f"alerts-dead:{uuid4()}").id for _ in range(count)]
        Job.objects.filter(pk__in=ids).update(state=Job.State.DEAD)


def test_dead_jobs_are_told_once_per_day_and_band(admin):
    assert alerts.dead_band(0) == 0 and alerts.dead_band(9) == 1 and alerts.dead_band(10) == 10
    dead_jobs(1)
    watch()
    watch()
    assert notices(alerts.JOBS_DEAD) == [{"count": 1}]
    dead_jobs(9)
    watch()
    assert sorted(item["count"] for item in notices(alerts.JOBS_DEAD)) == [1, 10]
    run_pending_jobs()
    with system_context(reason="test:platform-alerts-rows"):
        titles = set(PlatformNotificationRecipient.objects.filter(user=admin).values_list("title", flat=True))
    assert titles == {"Há 1 jobs que falharam", "Há 10 jobs que falharam"}


def test_a_late_queue_is_told_once_per_hour_and_never_behind_maintenance(admin, monkeypatch):
    from skeleton.jobs.services import enqueue

    with system_context(reason="test:platform-alerts-lag"):
        enqueue(alerts.WATCH_JOB, idempotency_key="alerts-lag")
    later = timezone.now() + timedelta(hours=2)
    monkeypatch.setattr("skeleton.jobs.worker.maintenance_paused", lambda: True)
    watch(later)
    assert notices(alerts.QUEUE_LAGGING) == []
    monkeypatch.setattr("skeleton.jobs.worker.maintenance_paused", lambda: False)
    watch(later)
    watch(later)
    assert len(notices(alerts.QUEUE_LAGGING)) == 1 and notices(alerts.QUEUE_LAGGING)[0]["minutes"] >= 119


def test_updates_and_failed_operations_reach_who_operates_updates(admin, monkeypatch):
    from skeleton.accounts.platform import grant_platform_role
    from skeleton.system.models import DeploymentOperation

    operator = member("platform-alerts-operator@example.test")
    plain = member("platform-alerts-plain@example.test")
    with request_context(actor=admin, request_id=uuid4(), source="test:platform-alerts-roles"):
        grant_platform_role(actor=admin, user=operator, role="operator")
        operation = DeploymentOperation(
            operation="update", expected_generation=1, plan_digest="c" * 64, plan={"generation": 1}
        )
        operation.save()
        operation.state = "failed"
        operation.save(update_fields={"state"})
    monkeypatch.setattr("skeleton.system.releases.update_notice", lambda: {"mode": "agent", "core": "0.8.0", "apps": 2})
    watch()
    watch()
    assert notices(alerts.UPDATE_AVAILABLE) == [{"core": "0.8.0", "apps": 2}]
    assert notices(alerts.OPERATION_FAILED) == [{"operation": str(operation.pk)}]
    run_pending_jobs()
    with system_context(reason="test:platform-alerts-who"):
        receivers = set(
            PlatformNotificationRecipient.objects.filter(type_code=alerts.OPERATION_FAILED).values_list(
                "user_id", flat=True
            )
        )
    assert receivers == {admin.pk, operator.pk} and plain.pk not in receivers


def test_a_suspended_organization_is_told_to_who_operates_billing(admin):
    from skeleton.insights.collect import refresh_organization
    from skeleton.insights.models import OrganizationSnapshot
    from skeleton.testing.people import make_organization

    organization = make_organization(owner=admin, slug="alerts-suspended", name="Suspensa")
    with system_context(reason="test:platform-alerts-snapshot", organization_id=organization.pk):
        refresh_organization()
    with system_context(reason="test:platform-alerts-suspend"):
        OrganizationSnapshot.all_objects.filter(subject_organization=organization).update(
            billing={"access_state": "suspended"}
        )
    watch()
    watch()
    assert notices(alerts.ORGANIZATION_SUSPENDED) == [{"organization": str(organization.pk)}]


def test_an_erasure_request_is_told_at_once_without_the_person(admin):
    from skeleton.accounts.erasure import request_erasure

    person = member("platform-alerts-erase@example.test")
    with request_context(actor=person, request_id=uuid4(), source="test:platform-alerts-erasure"):
        record = request_erasure(person)
    assert notices(alerts.ERASURE_REQUESTED) == [{"request": str(record.pk)}]
    assert person.email not in str(notices(alerts.ERASURE_REQUESTED))


def test_a_broken_check_never_stops_the_others_and_the_setting_turns_all_off(admin, monkeypatch, settings):
    def broken(now):
        raise RuntimeError("boom")

    dead_jobs(1)
    monkeypatch.setattr(alerts, "CHECKS", (("updates", broken), *alerts.CHECKS[:1]))
    assert watch() == ["updates"]
    assert notices(alerts.JOBS_DEAD) == [{"count": 1}]
    settings.SKELETON_PLATFORM_ALERTS_ENABLED = False
    dead_jobs(9)
    assert watch() == []
    assert len(notices(alerts.JOBS_DEAD)) == 1
