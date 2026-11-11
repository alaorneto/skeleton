"""Fan-out by user, group, role and organization with authorization on the target (#51)."""

import pytest

from skeleton.jobs.models import Job
from skeleton.notifications import fanout, registry
from skeleton.notifications.models import NotificationRecipient
from skeleton.notifications.registry import NotificationType, register_notification_type
from skeleton.notifications.services import Recipients, notify
from skeleton.tenancy.context import system_context
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.service_api import build_service_scenario

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
        NotificationType("notes.mention", "Menção", target_table_key="example_notes.note", group_key="note:{target}")
    )
    register_notification_type(NotificationType("system.notice", "Aviso"))
    return build_service_scenario(Note, slug="fanout")


def rows(scenario):
    with system_context(reason="test:fanout-rows", organization_id=scenario.a.pk):
        return sorted((row[0], row[1]) for row in NotificationRecipient.objects.values_list("user_id", "count"))


def send(scenario, kind, recipients, *, target=None, actor=None):
    with scenario.acting(actor or scenario.admin_a):
        return notify(kind, recipients=recipients, target=target, payload={})


def test_only_people_who_read_the_target_receive_a_row(scenario):
    shared = scenario.create(scenario.a, code="F-1", title="Shared", visibility="organization")
    private = scenario.create(scenario.a, code="F-2", title="Private", visibility="private")
    everyone = Recipients(users=(scenario.member.pk, scenario.denied.pk))
    send(scenario, "notes.mention", everyone, target=shared)
    run_pending_jobs()
    # denied has no TablePermission on notes: no row, even though named directly.
    assert rows(scenario) == [(scenario.member.pk, 1)]
    with system_context(reason="test:fanout-private", organization_id=scenario.a.pk):
        NotificationRecipient.objects.all().delete()
    send(scenario, "notes.mention", everyone, target=private, actor=scenario.denied)
    run_pending_jobs()
    assert rows(scenario) == [(scenario.member.pk, 1)]  # member owns the private note.


def test_removed_from_group_before_fan_out_receives_nothing(scenario):
    from skeleton.access.models import AccessGroup, GroupMember
    from skeleton.tenancy.models import Membership

    with system_context(reason="test:fanout-group", organization_id=scenario.a.pk):
        group = AccessGroup(organization=scenario.a, code="team", name="Team")
        group.save()
        link = GroupMember(
            organization=scenario.a, group=group, membership=Membership.objects.get(user=scenario.denied)
        )
        link.save()
        GroupMember(
            organization=scenario.a, group=group, membership=Membership.objects.get(user=scenario.member)
        ).save()
    send(scenario, "system.notice", Recipients(groups=(group.pk,)))
    with system_context(reason="test:fanout-ungroup", organization_id=scenario.a.pk):
        GroupMember.objects.get(pk=link.pk).delete()
    run_pending_jobs()
    assert rows(scenario) == [(scenario.member.pk, 1)]


def test_role_recipients_include_direct_holders(scenario):
    from skeleton.access.models import Role

    with system_context(reason="test:fanout-role", organization_id=scenario.a.pk):
        role = Role.objects.get(code="service-contract")
    send(scenario, "system.notice", Recipients(roles=(role.pk,)))
    run_pending_jobs()
    assert rows(scenario) == [(scenario.member.pk, 1)]


def test_same_group_key_collapses_into_one_unread_row(scenario):
    note = scenario.create(scenario.a, code="F-3", title="Grouped", visibility="organization")
    for _ in range(2):
        send(scenario, "notes.mention", Recipients(users=(scenario.member.pk,)), target=note)
        run_pending_jobs()
    assert rows(scenario) == [(scenario.member.pk, 2)]


def test_broadcast_reaches_every_active_member_but_the_actor(scenario):
    send(scenario, "system.notice", Recipients(organization=True))
    run_pending_jobs()
    assert {user for user, _ in rows(scenario)} == {scenario.member.pk, scenario.denied.pk}


def test_large_audiences_are_split_in_batches(scenario, monkeypatch):
    monkeypatch.setattr(fanout, "BATCH_SIZE", 1)
    send(scenario, "system.notice", Recipients(organization=True))
    run_pending_jobs()
    assert Job.all_objects.filter(job_type="notifications.fanout").count() == 2
    assert len(rows(scenario)) == 2
