"""Fan-out by user, group, role and organization with authorization on the target (#51)."""

import pytest

from skeleton.jobs.models import Job
from skeleton.notifications import fanout
from skeleton.notifications.models import NotificationRecipient
from skeleton.notifications.registry import NotificationType, register_notification_type
from skeleton.notifications.services import Recipients, notify
from skeleton.tenancy.context import system_context
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.registries import isolated_notification_types
from skeleton.testing.service_api import build_service_scenario

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]


@pytest.fixture
def scenario(monkeypatch):

    from examples.notes.models import Note

    with isolated_notification_types():
        register_notification_type(
            NotificationType(
                "notes.mention", "Menção", target_table_key="example_notes.note", group_key="note:{target}"
            )
        )
        register_notification_type(NotificationType("system.notice", "Aviso"))
        yield build_service_scenario(Note, slug="fanout")


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


def _members(scenario, count, start=0):
    from skeleton.access.models import Role
    from skeleton.access.services import provision_membership
    from skeleton.accounts.services import create_identity

    users = []
    with system_context(reason="test:fanout-members", organization_id=scenario.a.pk):
        role = Role.objects.get(code="service-contract")
        for index in range(start, start + count):
            user = create_identity(email=f"fan-{index}@fanout.example.test", password=None)
            provision_membership(organization=scenario.a, user=user, role=role)
            users.append(user)
    return users


def _authorization_queries(scenario, people, target):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with system_context(reason="test:fanout-cost", organization_id=scenario.a.pk):
        with CaptureQueriesContext(connection) as captured:
            fanout._authorized([user.pk for user in people], target)
    return [
        query["sql"]
        for query in captured
        if query["sql"].startswith("SELECT")
        and ("sk_access_" in query["sql"] or "sk_tenancy_membership" in query["sql"])
    ]


def test_fanout_authorization_queries_do_not_grow_with_recipients(scenario):
    """One batch decision, not one authorization per person (#296)."""
    shared = scenario.create(scenario.a, code="F-COST", title="Shared", visibility="organization")
    people = _members(scenario, 50)
    assert len(_authorization_queries(scenario, people[:5], shared)) == len(
        _authorization_queries(scenario, people, shared)
    )


def test_batches_of_500_chain_until_fanned_out(scenario, monkeypatch):
    monkeypatch.setattr(fanout, "BATCH_SIZE", 2)
    assert fanout.BATCH_SIZE == 2
    people = _members(scenario, 5, start=100)
    send(scenario, "system.notice", Recipients(users=tuple(user.pk for user in people)))
    outcomes = run_pending_jobs()
    assert len([outcome for outcome in outcomes if outcome.job_type == "notifications.fanout"]) == 3
    assert len(rows(scenario)) == 5


def test_existing_deliveries_are_not_recreated_on_rerun(scenario):
    from skeleton.notifications.channels.delivery import create_delivery
    from skeleton.notifications.models import NotificationDelivery

    with system_context(reason="test:fanout-rerun", organization_id=scenario.a.pk):
        assert create_delivery(
            organization_id=scenario.a.pk, user_id=scenario.member.pk, channel="email", dedupe_key="email:x:y"
        )
        assert (
            create_delivery(
                organization_id=scenario.a.pk, user_id=scenario.member.pk, channel="email", dedupe_key="email:x:y"
            )
            is None
        )
        assert NotificationDelivery.objects.filter(dedupe_key="email:x:y").count() == 1
