"""notify(): typed notifications created in the business transaction (#50).

Runs on SQLite and, in the PostgreSQL job, with the runtime role (RLS and self-discovery).
"""

import pytest
from django.core.exceptions import ValidationError

from skeleton.accounts.services import bootstrap_identity
from skeleton.common.exceptions import MissingTenantContext
from skeleton.jobs.models import Job
from skeleton.notifications import registry
from skeleton.notifications.models import Notification, NotificationRecipient
from skeleton.notifications.registry import NotificationType, notification_problems, register_notification_type
from skeleton.notifications.services import Recipients, notify
from skeleton.tenancy.context import own_rows, request_context, system_context, tenant_context
from skeleton.testing.jobs import run_pending_jobs

pytestmark = pytest.mark.usefixtures("skeleton_db")
PASSWORD = "Notify-Password!93"


@pytest.fixture(autouse=True)
def types(monkeypatch):
    from django.apps import apps

    if not apps.is_installed("examples.notes"):
        pytest.skip("SKELETON_ENABLE_EXAMPLE_APP=true is required")
    monkeypatch.setattr(registry, "_types", {})
    monkeypatch.setattr(registry, "_duplicates", [])
    register_notification_type(
        NotificationType(
            "notes.shared",
            "Nota compartilhada",
            payload_fields=("message",),
            snapshot_fields=("title", "code"),
            target_table_key="example_notes.note",
        )
    )
    register_notification_type(NotificationType("system.hello", "Olá", payload_fields=("message",)))


@pytest.fixture
def tenants():
    alice, alpha = bootstrap_identity(email="nt-a@example.test", organization="nt-a", name="A", password=PASSWORD)
    bob, beta = bootstrap_identity(email="nt-b@example.test", organization="nt-b", name="B", password=PASSWORD)
    from skeleton.access.models import Role
    from skeleton.access.services import provision_membership

    with system_context(reason="test:notify-join", organization_id=alpha.pk):
        provision_membership(organization=alpha, user=bob, role=Role.objects.get(code="member"))
    return alice, alpha, bob, beta


def note(organization, owner):
    from examples.notes.models import Note

    record = Note(organization=organization, owner=owner, code="NT-1", title="Shared title", body="secret body")
    record.save()
    return record


def test_notify_records_snapshot_and_enqueues_fan_out(tenants):
    alice, alpha, bob, _ = tenants
    with tenant_context(organization_id=alpha.pk, actor=alice, source="test:notify"):
        notification = notify(
            "notes.shared", recipients=Recipients(users=(bob.pk,)), target=note(alpha, alice), payload={"message": "hi"}
        )
        assert notification.snapshot == {"title": "Shared title", "code": "NT-1"}
        assert "body" not in notification.snapshot
        # Without a target, the fan-out needs no read authorization (see test_notifications_fanout).
        notify("system.hello", recipients=Recipients(users=(bob.pk,)), payload={"message": "hi"})
    assert Job.all_objects.filter(job_type="notifications.fanout").count() == 2
    run_pending_jobs()
    with tenant_context(organization_id=alpha.pk, actor=bob, source="test:notify-read"):
        # Bob cannot read Alice's private note: only the notification without target reaches him.
        rows = list(NotificationRecipient.objects.values_list("user_id", "notification__type_code"))
        assert rows == [(bob.pk, "system.hello")]


def test_rollback_leaves_no_notification_and_no_job(tenants):
    alice, alpha, bob, _ = tenants
    with pytest.raises(RuntimeError), tenant_context(organization_id=alpha.pk, actor=alice, source="test:notify-rb"):
        notify("system.hello", recipients=Recipients(users=(bob.pk,)), payload={"message": "x"})
        raise RuntimeError("business failure")
    assert not Job.all_objects.filter(job_type="notifications.fanout").exists()
    with tenant_context(organization_id=alpha.pk, actor=alice, source="test:notify-rb-check"):
        assert not Notification.objects.exists()


def test_invalid_payload_recipients_and_context_are_refused(tenants):
    from skeleton.access.models import AccessGroup, Role

    alice, alpha, bob, beta = tenants
    with tenant_context(organization_id=beta.pk, actor=bob, source="test:notify-b"):
        foreign_group = AccessGroup(organization=beta, code="b-team", name="B team")
        foreign_group.save()
        foreign_role = Role.objects.get(code="member")
    with tenant_context(organization_id=alpha.pk, actor=alice, source="test:notify-invalid"):
        cases = [
            dict(recipients=Recipients(users=(bob.pk,)), payload={"secret": "x"}),
            dict(recipients=Recipients(groups=(foreign_group.pk,)), payload={}),
            dict(recipients=Recipients(roles=(foreign_role.pk,)), payload={}),
            dict(recipients=Recipients(), payload={}),
        ]
        for case in cases:
            with pytest.raises(ValidationError):
                notify("system.hello", **case)
        with pytest.raises(ValidationError):
            notify("unknown.type", recipients=Recipients(users=(bob.pk,)))
        with pytest.raises(ValidationError):
            notify("notes.shared", recipients=Recipients(users=(bob.pk,)))
    with request_context(actor=alice, request_id=None, source="test:notify-global"):
        with pytest.raises(MissingTenantContext):
            notify("system.hello", recipients=Recipients(users=(bob.pk,)))


def test_dedupe_key_returns_the_existing_notification(tenants):
    alice, alpha, bob, _ = tenants
    with tenant_context(organization_id=alpha.pk, actor=alice, source="test:notify-dedupe"):
        first = notify("system.hello", recipients=Recipients(users=(bob.pk,)), dedupe_key="hello:1")
        second = notify("system.hello", recipients=Recipients(users=(bob.pk,)), dedupe_key="hello:1")
    assert first.pk == second.pk
    assert Job.all_objects.filter(job_type="notifications.fanout").count() == 1


def test_person_discovers_own_rows_across_organizations_only(tenants):
    alice, alpha, bob, beta = tenants
    with tenant_context(organization_id=alpha.pk, actor=alice, source="test:notify-a"):
        notify("system.hello", recipients=Recipients(users=(bob.pk,)))
    with tenant_context(organization_id=beta.pk, actor=bob, source="test:notify-b-self"):
        notify("system.hello", recipients=Recipients(organization=True))
    run_pending_jobs()
    with request_context(actor=bob, request_id=None, source="test:notify-inbox"):
        mine = set(own_rows(NotificationRecipient, bob).values_list("organization_id", flat=True))
    # Bob was notified in Alpha; in Beta he is the actor of the broadcast and is excluded.
    assert mine == {alpha.pk}
    with tenant_context(organization_id=beta.pk, actor=bob, source="test:notify-b-manager"):
        assert not NotificationRecipient.objects.filter(organization_id=alpha.pk).exists()


def test_declarations_with_sensitive_fields_fail_the_check():
    register_notification_type(
        NotificationType("notes.leak", "Leak", snapshot_fields=("body",), target_table_key="example_notes.note")
    )
    register_notification_type(
        NotificationType("notes.leak2", "Leak", payload_fields=("body",), target_table_key="example_notes.note")
    )
    problems = "\n".join(notification_problems())
    assert "notes.leak: snapshot field body is sensitive" in problems
    assert "notes.leak2: payload field body is sensitive" in problems
