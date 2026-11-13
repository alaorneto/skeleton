"""NotificationService of the core service API (#107)."""

import pytest

from skeleton.notifications.models import Notification
from skeleton.notifications.registry import NotificationType, register_notification_type
from skeleton.service_api.errors import Forbidden, InvalidInput, NotFound
from skeleton.service_api.notifications import NotificationService, Recipients
from skeleton.tenancy.context import request_context, system_context
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.registries import isolated_notification_types
from skeleton.testing.service_api import (
    acting_as,
    assert_rejects_other_actor,
    assert_service_contract,
    build_service_scenario,
)

pytestmark = pytest.mark.usefixtures("skeleton_db")
NOTES_MODULE = "examples.notes.services"


def as_module(module, function):
    namespace = {"__name__": module, "function": function}
    exec("def call(*args, **kwargs):\n    return function(*args, **kwargs)\n", namespace)  # noqa: S102
    return namespace["call"]


def test_contract():
    assert_service_contract(NotificationService)


@pytest.fixture
def scenario(monkeypatch):

    from examples.notes.models import Note

    with isolated_notification_types():
        register_notification_type(
            NotificationType(
                "notes.shared",
                "Nota compartilhada",
                payload_fields=("message",),
                snapshot_fields=("title",),
                target_table_key="example_notes.note",
                owner_app="example_notes",
            )
        )
        register_notification_type(NotificationType("other.ping", "Ping", owner_app="other_app"))
        register_notification_type(NotificationType("orphan.ping", "Ping"))  # No owner_app (#272).
        yield build_service_scenario(Note, slug="notify-service")


def notify_as_notes(**kwargs):
    return as_module(NOTES_MODULE, NotificationService().notify)(**kwargs)


@pytest.mark.needs_example_app
def test_owning_app_notifies_and_the_recipient_reads_marks_and_archives(scenario):
    note = scenario.create(scenario.a, owner=scenario.admin_a, code="NS-1", title="Plan", visibility="organization")
    with acting_as(scenario.admin_a, scenario.a) as admin:
        receipt = notify_as_notes(
            actor=admin,
            type_code="notes.shared",
            recipients=Recipients(users=(scenario.member.pk,)),
            payload=(("message", "Veja"),),
            target_table_key="example_notes.note",
            target_id=note.pk,
        )
    assert receipt.type_code == "notes.shared"
    run_pending_jobs()
    service = NotificationService()
    with request_context(actor=scenario.member, request_id=None, source="test:notify-service-global"):
        assert service.unread_count(actor=scenario.member) == 1
        page = service.inbox(actor=scenario.member)
    [item] = page.items
    assert (item.label, item.snapshot[0].value, item.payload[0].value) == ("Nota compartilhada", "Plan", "Veja")
    with acting_as(scenario.member, scenario.a) as actor:
        assert service.mark_read(actor=actor, ids=(item.sysid,)) == 1
        assert service.mark_read(actor=actor, ids=(item.sysid,)) == 0
        assert service.archive(actor=actor, ids=(item.sysid,)) == 1
        assert service.inbox(actor=actor).items == ()
        assert len(service.inbox(actor=actor, include_archived=True).items) == 1
    with acting_as(scenario.denied, scenario.a) as other, pytest.raises(NotFound):
        service.mark_read(actor=other, ids=(item.sysid,))


@pytest.mark.needs_example_app
def test_another_apps_type_is_forbidden(scenario):
    with acting_as(scenario.admin_a, scenario.a) as admin:
        with pytest.raises(Forbidden):
            notify_as_notes(actor=admin, type_code="other.ping", recipients=Recipients(users=(scenario.member.pk,)))
        with pytest.raises(Forbidden):  # Called from a module outside the owning app.
            NotificationService().notify(
                actor=admin,
                type_code="notes.shared",
                recipients=Recipients(users=(scenario.member.pk,)),
                target_table_key="example_notes.note",
                target_id=scenario.member.pk,
            )
        with pytest.raises(InvalidInput):
            notify_as_notes(actor=admin, type_code="unknown", recipients=Recipients(users=(scenario.member.pk,)))


@pytest.mark.needs_example_app
def test_unreadable_or_foreign_target_is_not_found(scenario):
    hidden = scenario.create(scenario.a, owner=scenario.admin_a, code="NS-2", title="Hidden", visibility="private")
    foreign = scenario.create(scenario.b, owner=scenario.member, code="NS-3", title="B")
    with acting_as(scenario.member, scenario.a) as actor:
        for target in (hidden, foreign):
            with pytest.raises(NotFound):
                notify_as_notes(
                    actor=actor,
                    type_code="notes.shared",
                    recipients=Recipients(users=(scenario.denied.pk,)),
                    target_table_key="example_notes.note",
                    target_id=target.pk,
                )


@pytest.mark.needs_example_app
def test_caller_rollback_leaves_no_notification_and_system_consumers_need_no_actor(scenario):
    note = scenario.create(scenario.a, owner=scenario.admin_a, code="NS-4", title="T", visibility="organization")
    kwargs = {
        "type_code": "notes.shared",
        "recipients": Recipients(users=(scenario.member.pk,)),
        "target_table_key": "example_notes.note",
        "target_id": note.pk,
    }
    with pytest.raises(RuntimeError), acting_as(scenario.admin_a, scenario.a) as admin:
        notify_as_notes(actor=admin, **kwargs)
        raise RuntimeError("caller failure")
    with system_context(reason="test:notify-service-consumer", organization_id=scenario.a.pk):
        assert not Notification.objects.exists()
        notify_as_notes(actor=None, **kwargs)
        assert Notification.objects.count() == 1
    assert_rejects_other_actor(scenario.caller(lambda actor: NotificationService().unread_count(actor=scenario.denied)))


@pytest.mark.needs_example_app
def test_type_without_owner_is_forbidden_for_everyone(scenario):
    """A type without owner_app is usable by no caller of the service, app or not (#272)."""
    recipients = Recipients(users=(scenario.member.pk,))
    with acting_as(scenario.admin_a, scenario.a) as admin:
        with pytest.raises(Forbidden):
            NotificationService().notify(actor=admin, type_code="orphan.ping", recipients=recipients)
        with pytest.raises(Forbidden):
            notify_as_notes(actor=admin, type_code="orphan.ping", recipients=recipients)


@pytest.mark.needs_example_app
def test_inbox_and_unread_count_are_tenant_isolated(scenario):
    """The member has notifications in A and B; the context of A shows only A's (#319)."""
    from skeleton.testing.service_api import assert_tenant_isolated

    for organization, admin, code in ((scenario.a, scenario.admin_a, "ISO-A"), (scenario.b, scenario.admin_b, "ISO-B")):
        note = scenario.create(organization, owner=admin, code=code, title=code, visibility="organization")
        with acting_as(admin, organization) as actor:
            notify_as_notes(
                actor=actor,
                type_code="notes.shared",
                recipients=Recipients(users=(scenario.member.pk,)),
                payload=(("message", code),),
                target_table_key="example_notes.note",
                target_id=note.pk,
            )
    run_pending_jobs()
    service = NotificationService()
    with system_context(reason="test:inbox-foreign", organization_id=scenario.b.pk):
        from skeleton.notifications.models import NotificationRecipient

        foreign = (
            NotificationRecipient.all_objects.filter(user=scenario.member, organization=scenario.b)
            .values_list("sysid", flat=True)
            .first()
        )
    assert foreign is not None
    page = assert_tenant_isolated(scenario.caller(lambda actor: service.inbox(actor=actor)), foreign=[foreign, "ISO-B"])
    assert [item.organization_id for item in page.items] == [scenario.a.pk]
    # Documented: the count is the person's own unread rows in every organization with an active
    # membership (the bell of the shell), so it is 2 here; it never reaches another person's rows.
    assert scenario.caller(lambda actor: service.unread_count(actor=actor))() == 2
    assert scenario.caller(lambda actor: service.unread_count(actor=actor), user=scenario.denied)() == 0
    with acting_as(scenario.member, scenario.a) as actor, pytest.raises(NotFound):
        service.mark_read(actor=actor, ids=(foreign,))
