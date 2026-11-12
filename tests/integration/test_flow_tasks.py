"""Human steps (#204): queues, forms, approvals with every policy, delegation and deadlines.

PostgreSQL with the runtime role; flows are registered for the test, about example notes.
"""

from uuid import uuid4

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from skeleton.access.models import AccessGroup, GroupMember, MemberRole, Role
from skeleton.audit.models import AuditEvent
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.flows import policies, services, tasks
from skeleton.flows import registry as flows
from skeleton.flows.errors import FlowConflict
from skeleton.flows.models import FlowActivity, FlowInstance, FlowSubmission, FlowTask
from skeleton.flows.services import NotVisible
from skeleton.jobs.models import Job
from skeleton.notifications.models import NotificationRecipient
from skeleton.tenancy.context import system_context
from skeleton.testing.jobs import run_pending_jobs
from tools.security_fixtures import actor_tenant, build_security_fixture

pytestmark = pytest.mark.needs_example_app

OWNER = "example_notes"
SECRET = "conta 0001-9 segredo"
FILL = flows.Form(
    flows.Field("audience", "choice", required=True, choices=("internal", "public")),
    flows.Field("amount", "decimal", minimum=0),
    flows.Field("account", "str", sensitive=True),
    flows.Field("approver", "user"),
)
REASON = flows.Form(flows.Field("reason", "text", required=True, max_length=200))
OPINION = flows.Form(flows.Field("opinion", "text"))


def spec(key, approval, **overrides):
    """start -> fill (starter) -> approval step -> done | back to fill on rejection."""
    values = {
        "key": f"example_notes.{key}",
        "version": 1,
        "label": f"Fluxo {key}",
        "owner_app": OWNER,
        "subject_table": "example_notes.note",
        "steps": (
            flows.start("start"),
            flows.task("fill", label="Preencher", assignees=(flows.Assignment.starter(),), form=FILL),
            approval,
            flows.end("done", outcome="approved"),
            flows.end("dropped", outcome="dropped"),
        ),
        "transitions": (
            flows.Transition("start", "fill"),
            flows.Transition("fill", "review", key="submit", label="Enviar"),
            flows.Transition("fill", "dropped", key="drop", label="Desistir", form=REASON),
            flows.Transition("review", "done", on="approved"),
            flows.Transition("review", "fill", on="rejected"),
        ),
    }
    values.update(overrides)
    return flows.FlowSpec(**values)


def review(**kwargs):
    kwargs.setdefault("assignees", (flows.Assignment.role("note_reviewer"),))
    return flows.approval("review", label="Revisar", form=OPINION, **kwargs)


@pytest.fixture
def world(skeleton_db, monkeypatch):
    from skeleton.flows import registry

    for name in ("_flows", "_handlers", "_conditions", "_assignments"):
        monkeypatch.setattr(registry, name, {})
    monkeypatch.setattr(registry, "_duplicates", [])
    board = flows.Assignment.group("board", expand=True)
    flows.register_flow(spec("queue", review()))
    flows.register_flow(spec("all", review(assignees=(board,), policy="all")))
    flows.register_flow(spec("quorum", review(assignees=(board,), policy="quorum", quorum=2)))
    flows.register_flow(spec("sequential", review(assignees=(board,), policy="sequential")))
    flows.register_flow(spec("chosen", review(assignees=(flows.Assignment.field("approver"),), delegable=False)))
    flows.register_flow(
        spec(
            "sla",
            review(
                sla=flows.Sla(
                    due_seconds=3600,
                    remind_before_seconds=(600,),
                    on_overdue="reassign",
                    escalate_to=(flows.Assignment.subject_owner(),),
                )
            ),
        )
    )
    expiring = spec(
        "expiring",
        review(sla=flows.Sla(due_seconds=3600, on_overdue="expire")),
    )
    flows.register_flow(
        flows.FlowSpec(
            **{
                **{name: getattr(expiring, name) for name in ("key", "version", "label", "owner_app", "subject_table")},
                "steps": expiring.steps,
                "transitions": (*expiring.transitions, flows.Transition("review", "dropped", on="expired")),
            }
        )
    )
    assert flows.flow_problems() == []
    fixture = build_security_fixture()
    with system_context(reason="test:flow-people", organization_id=fixture.a.pk):
        reviewer = Role(organization=fixture.a, code="note_reviewer", name="Revisor")
        reviewer.save()
        MemberRole(organization=fixture.a, membership=fixture.bob_membership, role=reviewer).save()
        board_group = AccessGroup(organization=fixture.a, code="board", name="Diretoria")
        board_group.save()
        from skeleton.tenancy.models import Membership

        admin_membership = Membership.objects.get(user=fixture.admin)
        for membership in (fixture.alice_membership, fixture.bob_membership, admin_membership):
            GroupMember(organization=fixture.a, group=board_group, membership=membership).save()
    fixture.reviewer, fixture.board = reviewer, board_group
    return fixture


def start(world, key, note=None, user=None):
    user = user or world.alice
    with actor_tenant(user, world.a):
        instance, _ = services.start_instance(f"example_notes.{key}", subject_id=(note or world.private).pk, actor=user)
        return instance.pk


def open_tasks(world, instance_id, step=None):
    with actor_tenant(world.admin, world.a):
        rows = FlowTask.objects.filter(instance_id=instance_id, status="open").order_by("slot", "created_at")
        return [task for task in rows if step in (None, task.step_key)]


def act(world, user, function, task, **kwargs):
    kwargs.setdefault("expected_revision", task.revision)
    with actor_tenant(user, world.a):
        return function(task.pk, actor=user, **kwargs)


def fill(world, instance_id, user=None, **data):
    [task] = open_tasks(world, instance_id, "fill")
    data.setdefault("audience", "public")
    return act(world, user or world.alice, tasks.complete_task, task, action="submit", data=data)


def instance_of(world, instance_id):
    with actor_tenant(world.admin, world.a):
        return FlowInstance.objects.get(pk=instance_id)


def kinds(world, instance_id):
    with actor_tenant(world.admin, world.a):
        return list(
            FlowActivity.objects.filter(instance_id=instance_id).order_by("sequence").values_list("kind", flat=True)
        )


def inbox(world, user):
    with actor_tenant(user, world.a):
        return list(NotificationRecipient.objects.filter(user=user).values_list("type_code", flat=True))


def test_form_is_an_allowlist_validated_on_the_server(world):
    instance_id = start(world, "queue")
    [task] = open_tasks(world, instance_id)
    assert task.assignee_id == world.alice.pk and task.kind == "task"
    bad = (
        {"audience": "public", "is_admin": True},  # Not declared.
        {},  # Required field missing.
        {"audience": "elsewhere"},
        {"audience": "public", "amount": "-1"},
        {"audience": "public", "amount": "abc"},
        {"audience": "public", "approver": str(world.outsider.pk)},  # Not a member of this organization.
        {"audience": "public", "reason": "x"},  # Belongs to the other transition's form.
    )
    for data in bad:
        with pytest.raises(ValidationError):
            act(world, world.alice, tasks.complete_task, task, action="submit", data=data)
    with pytest.raises(ValidationError):
        act(world, world.alice, tasks.complete_task, task, action="nonexistent", data={"audience": "public"})
    with pytest.raises(ValidationError):
        act(world, world.alice, tasks.complete_task, task, data={"audience": "public"})  # Two actions: choose.
    with pytest.raises(ValidationError):
        act(world, world.alice, tasks.complete_task, task, action="drop", data={"audience": "public"})
    assert [item.pk for item in open_tasks(world, instance_id)] == [task.pk]
    with actor_tenant(world.admin, world.a):
        assert not FlowSubmission.objects.exists()

    done = fill(world, instance_id, amount="10.5", account=SECRET)
    assert (done.status, done.action, done.completed_by_id) == ("completed", "submit", world.alice.pk)
    instance = instance_of(world, instance_id)
    assert instance.data == {"audience": "public", "amount": "10.5"}
    assert instance.sensitive_data == {"account": SECRET}
    with actor_tenant(world.admin, world.a):
        submission = FlowSubmission.objects.get(instance_id=instance_id)
        assert (submission.origin, submission.transition_key) == ("task", "submit")
        assert submission.sensitive_values == {"account": SECRET} and "account" not in submission.values
        assert SECRET not in str(list(AuditEvent.objects.values_list("changes", flat=True)))


def test_queue_task_is_claimed_released_and_decided_only_by_its_candidates(world):
    instance_id = start(world, "queue")
    fill(world, instance_id)
    [task] = open_tasks(world, instance_id, "review")
    assert task.assignee_id is None and task.candidate_role_id == world.reviewer.pk
    with actor_tenant(world.bob, world.a):
        assert list(policies.actionable_tasks(world.bob).values_list("pk", flat=True)) == [task.pk]
    with actor_tenant(world.alice, world.a):  # She started it, so she reads the instance, not the queue.
        assert not policies.actionable_tasks(world.alice).exists()
        assert policies.visible_instances(world.alice).filter(pk=instance_id).exists()
    for function, kwargs in ((tasks.claim_task, {}), (tasks.decide_task, {"decision": "approved"})):
        with pytest.raises(NotVisible) as hidden:
            act(world, world.alice, function, task, **kwargs)
        with pytest.raises(NotVisible) as missing, actor_tenant(world.alice, world.a):
            function(uuid4(), actor=world.alice, expected_revision=1, **kwargs)
        assert str(hidden.value) == str(missing.value)

    claimed = act(world, world.bob, tasks.claim_task, task)
    assert claimed.assignee_id == world.bob.pk
    with pytest.raises(ConcurrentUpdate):
        act(world, world.bob, tasks.claim_task, task)  # The revision he first saw is stale now.
    with pytest.raises(FlowConflict):
        act(world, world.bob, tasks.claim_task, claimed)
    released = act(world, world.bob, tasks.release_task, claimed)
    assert released.assignee_id is None
    with pytest.raises(FlowConflict):
        act(world, world.bob, tasks.release_task, released)

    # Losing the role loses the queue on the very next call.
    with system_context(reason="test:flow-revoke", organization_id=world.a.pk):
        MemberRole.objects.get(role=world.reviewer).delete()
    with pytest.raises(NotVisible):
        act(world, world.bob, tasks.claim_task, released)
    with system_context(reason="test:flow-revoke", organization_id=world.a.pk):
        MemberRole(organization=world.a, membership=world.bob_membership, role=world.reviewer).save()

    with pytest.raises(ValidationError):
        act(world, world.bob, tasks.decide_task, released, decision="maybe")
    with pytest.raises(FlowConflict):
        act(world, world.bob, tasks.complete_task, released)  # An approval is decided, not completed.
    decided = act(world, world.bob, tasks.decide_task, released, decision="approved", comment="ok " + SECRET)
    assert (decided.decision, decided.assignee_id, decided.candidate_role_id) == ("approved", world.bob.pk, None)
    instance = instance_of(world, instance_id)
    assert (instance.status, instance.outcome) == ("completed", "approved")
    with pytest.raises(FlowConflict):
        act(world, world.bob, tasks.decide_task, decided, decision="rejected")
    with actor_tenant(world.admin, world.a):
        assert SECRET not in str(list(AuditEvent.objects.values_list("changes", flat=True)))
        world.reviewer.refresh_from_db()
    assert "task_claimed" in kinds(world, instance_id) and "task_released" in kinds(world, instance_id)


def test_the_loser_of_a_queue_race_gets_a_conflict_not_a_missing_task(world):
    with system_context(reason="test:flow-queue-race", organization_id=world.a.pk):
        MemberRole(organization=world.a, membership=world.alice_membership, role=world.reviewer).save()
    instance_id = start(world, "queue")
    fill(world, instance_id)
    [task] = open_tasks(world, instance_id, "review")
    assert task.candidate_role_id == world.reviewer.pk
    act(world, world.bob, tasks.decide_task, task, decision="approved")
    with pytest.raises(ConcurrentUpdate):  # Alice read the same revision as Bob.
        act(world, world.alice, tasks.decide_task, task, decision="rejected")
    with actor_tenant(world.admin, world.a):
        closed = FlowTask.objects.get(pk=task.pk)
        assert (closed.status, closed.candidate_role_id) == ("completed", None)  # The queue was dropped.
    with pytest.raises(FlowConflict) as late:  # With the current revision: already closed.
        act(world, world.alice, tasks.decide_task, closed, decision="rejected")
    assert late.value.code == "task_closed"
    with actor_tenant(world.alice, world.a):
        assert policies.visible_tasks(world.alice).filter(pk=task.pk).exists()  # Still readable to the queue.
        assert not policies.actionable_tasks(world.alice).filter(pk=task.pk).exists()
    with actor_tenant(world.admin, world.a):
        assert FlowTask.objects.get(pk=task.pk).decision == "approved"
    # Out of the queue, the closed task is as good as missing again.
    with system_context(reason="test:flow-queue-race", organization_id=world.a.pk):
        MemberRole.objects.get(role=world.reviewer, membership=world.alice_membership).delete()
    with pytest.raises(NotVisible):
        act(world, world.alice, tasks.decide_task, closed, decision="rejected")
    with actor_tenant(world.alice, world.a):
        assert not policies.visible_tasks(world.alice).filter(pk=task.pk).exists()
    # The role can be removed: closed tasks no longer hold it.
    with system_context(reason="test:flow-queue-race", organization_id=world.a.pk):
        MemberRole.objects.get(role=world.reviewer, membership=world.bob_membership).delete()
        reviewer = Role.objects.get(pk=world.reviewer.pk)
        reviewer.delete()
        assert not Role.objects.filter(pk=world.reviewer.pk).exists()


def test_two_people_deciding_the_same_task_have_one_winner(world):
    instance_id = start(world, "all")
    fill(world, instance_id)
    mine = next(task for task in open_tasks(world, instance_id, "review") if task.assignee_id == world.bob.pk)
    act(world, world.bob, tasks.decide_task, mine, decision="approved")
    with pytest.raises(ConcurrentUpdate):
        act(world, world.bob, tasks.decide_task, mine, decision="rejected")  # Same revision, second write.
    with actor_tenant(world.admin, world.a):
        assert FlowTask.objects.get(pk=mine.pk).decision == "approved"


def test_rejection_returns_to_the_previous_step_and_keeps_the_history(world):
    instance_id = start(world, "queue")
    fill(world, instance_id, amount="1")
    [task] = open_tasks(world, instance_id, "review")
    act(world, world.bob, tasks.decide_task, task, decision="rejected", comment="faltou o valor", data={"opinion": "x"})
    [again] = open_tasks(world, instance_id)
    assert (again.step_key, again.assignee_id) == ("fill", world.alice.pk)
    assert instance_of(world, instance_id).status == "running"
    fill(world, instance_id, amount="2")
    with actor_tenant(world.admin, world.a):
        amounts = [
            item.values.get("amount")
            for item in FlowSubmission.objects.filter(instance_id=instance_id, origin="task").order_by("created_at")
        ]
        assert amounts == ["1", "2"]  # Both submissions are kept; the instance holds the latest.
        rejection = FlowActivity.objects.get(instance_id=instance_id, kind="task_decided")
        assert rejection.comment == "faltou o valor" and rejection.created_by == world.bob.pk
    assert instance_of(world, instance_id).data["amount"] == "2"
    [task] = open_tasks(world, instance_id, "review")
    act(world, world.bob, tasks.decide_task, task, decision="rejected")
    [task] = open_tasks(world, instance_id, "fill")
    act(world, world.alice, tasks.complete_task, task, action="drop", data={"audience": "public", "reason": "desisti"})
    assert instance_of(world, instance_id).outcome == "dropped"


def decide(world, instance_id, user, decision):
    task = next(task for task in open_tasks(world, instance_id, "review") if task.assignee_id == user.pk)
    return act(world, user, tasks.decide_task, task, decision=decision)


def test_policy_all_needs_everyone_and_one_rejection_rejects(world):
    instance_id = start(world, "all")
    fill(world, instance_id)
    assert {task.assignee_id for task in open_tasks(world, instance_id)} == {
        world.alice.pk,
        world.bob.pk,
        world.admin.pk,
    }
    decide(world, instance_id, world.alice, "approved")
    decide(world, instance_id, world.bob, "approved")
    assert instance_of(world, instance_id).status == "running"
    decide(world, instance_id, world.admin, "approved")
    assert instance_of(world, instance_id).outcome == "approved"

    other = start(world, "all", note=world.shared)
    fill(world, other)
    decide(world, other, world.bob, "rejected")
    assert [task.step_key for task in open_tasks(world, other)] == ["fill"]  # The others were cancelled.
    assert "task_cancelled" in kinds(world, other)


def test_policy_quorum_resolves_as_soon_as_the_count_is_decided(world):
    instance_id = start(world, "quorum")
    fill(world, instance_id)
    decide(world, instance_id, world.alice, "rejected")
    assert instance_of(world, instance_id).status == "running"
    decide(world, instance_id, world.bob, "approved")
    decide(world, instance_id, world.admin, "approved")
    assert instance_of(world, instance_id).outcome == "approved"

    other = start(world, "quorum", note=world.shared)
    fill(world, other)
    decide(world, other, world.alice, "rejected")
    decide(world, other, world.bob, "rejected")  # Two approvals are no longer possible.
    assert [task.step_key for task in open_tasks(world, other)] == ["fill"]

    third = start(world, "quorum", note=world.grouped)
    fill(world, third)
    decide(world, third, world.alice, "approved")
    decide(world, third, world.bob, "approved")  # The third opinion is not needed.
    assert instance_of(world, third).outcome == "approved" and open_tasks(world, third) == []


def test_policy_sequential_opens_one_approver_at_a_time(world):
    instance_id = start(world, "sequential")
    fill(world, instance_id)
    seen = []
    for _ in range(3):
        [task] = open_tasks(world, instance_id, "review")
        seen.append(task.assignee_id)
        user = next(user for user in (world.alice, world.bob, world.admin) if user.pk == task.assignee_id)
        act(world, user, tasks.decide_task, task, decision="approved")
    assert len(set(seen)) == 3 and instance_of(world, instance_id).outcome == "approved"

    other = start(world, "sequential", note=world.shared)
    fill(world, other)
    [task] = open_tasks(world, other, "review")
    user = next(user for user in (world.alice, world.bob, world.admin) if user.pk == task.assignee_id)
    act(world, user, tasks.decide_task, task, decision="rejected")
    assert [item.step_key for item in open_tasks(world, other)] == ["fill"]


def test_assignment_from_a_form_field_and_delegation_rules(world):
    instance_id = start(world, "chosen")
    fill(world, instance_id, approver=str(world.bob.pk))
    [task] = open_tasks(world, instance_id, "review")
    assert task.assignee_id == world.bob.pk
    with pytest.raises(PermissionDenied):  # The step is not delegable.
        act(world, world.bob, tasks.reassign_task, task, to_user_id=world.alice.pk)
    with pytest.raises(ValidationError):
        act(world, world.admin, tasks.reassign_task, task, to_user_id=world.outsider.pk)
    moved = act(world, world.admin, tasks.reassign_task, task, to_user_id=world.alice.pk, comment="férias")
    assert (moved.assignee_id, moved.delegated_from_id) == (world.alice.pk, world.bob.pk)
    with pytest.raises(PermissionDenied):  # Bob took part, so he still reads it, but it is not his any more.
        act(world, world.bob, tasks.decide_task, moved, decision="approved")
    act(world, world.alice, tasks.decide_task, moved, decision="approved")
    assert instance_of(world, instance_id).outcome == "approved"
    run_pending_jobs()
    assert "flows.task_assigned" in inbox(world, world.bob)


def test_delegable_task_can_be_handed_over_by_its_assignee(world):
    instance_id = start(world, "queue")
    [task] = open_tasks(world, instance_id)
    moved = act(world, world.alice, tasks.reassign_task, task, to_user_id=world.bob.pk)
    assert moved.assignee_id == world.bob.pk
    with pytest.raises(PermissionDenied):
        act(world, world.alice, tasks.complete_task, moved, action="submit", data={"audience": "public"})
    act(world, world.bob, tasks.complete_task, moved, action="submit", data={"audience": "public"})


def test_unresolved_assignment_leaves_a_task_for_the_administrator(world):
    with system_context(reason="test:flow-revoke", organization_id=world.a.pk):
        MemberRole.objects.get(role=world.reviewer).delete()
        world.reviewer.refresh_from_db()
        world.reviewer.delete()
    instance_id = start(world, "queue")
    fill(world, instance_id)
    [task] = open_tasks(world, instance_id, "review")
    assert (task.assignee_id, task.candidate_role_id, task.candidate_group_id) == (None, None, None)
    assert "assignment_unresolved" in kinds(world, instance_id)
    with pytest.raises(NotVisible):
        act(world, world.bob, tasks.claim_task, task)
    moved = act(world, world.admin, tasks.reassign_task, task, to_user_id=world.bob.pk)
    act(world, world.bob, tasks.decide_task, moved, decision="approved")
    assert instance_of(world, instance_id).outcome == "approved"


def test_suspended_cancelled_and_other_tenant_refuse_task_actions(world):
    instance_id = start(world, "queue")
    [task] = open_tasks(world, instance_id)
    revision = instance_of(world, instance_id).revision
    with actor_tenant(world.admin, world.a):
        services.suspend_instance(instance_id, actor=world.admin, expected_revision=revision)
    with pytest.raises(FlowConflict) as suspended:
        act(world, world.alice, tasks.complete_task, task, action="submit", data={"audience": "public"})
    assert suspended.value.code == "instance_suspended"
    with actor_tenant(world.admin, world.a):
        current = FlowInstance.objects.get(pk=instance_id)
        services.cancel_instance(instance_id, actor=world.admin, expected_revision=current.revision)
        task = FlowTask.objects.get(pk=task.pk)
        assert task.status == "cancelled"
    with pytest.raises(FlowConflict):
        act(world, world.alice, tasks.complete_task, task, action="submit", data={"audience": "public"})
    # Organization B never sees it, whoever asks.
    with actor_tenant(world.bob, world.b):
        assert not policies.visible_tasks(world.bob).exists() and not FlowTask.objects.exists()
        with pytest.raises(NotVisible):
            tasks.complete_task(task.pk, actor=world.bob, expected_revision=task.revision)
    with actor_tenant(world.alice, world.b):
        with pytest.raises(NotVisible):
            tasks.complete_task(task.pk, actor=world.alice, expected_revision=task.revision)


def due_now(job_type):
    Job.all_objects.filter(job_type=job_type, state="queued").update(run_after=timezone.now())


def test_sla_reminds_once_and_reassigns_an_overdue_task(world):
    instance_id = start(world, "sla")
    fill(world, instance_id)
    run_pending_jobs()  # Fan-out of the assignment notification.
    [task] = open_tasks(world, instance_id, "review")
    assert task.due_at is not None and inbox(world, world.bob) == ["flows.task_assigned"]
    due_now("flows.task_reminder")
    run_pending_jobs()
    [task] = open_tasks(world, instance_id, "review")
    assert task.reminded_count == 1 and kinds(world, instance_id).count("task_reminded") == 1
    due_now("flows.task_due")
    run_pending_jobs()
    [task] = open_tasks(world, instance_id, "review")
    assert task.escalated_at is not None
    assert (task.assignee_id, task.candidate_role_id) == (world.alice.pk, None)  # The owner of the note.
    run_pending_jobs()
    assert "flows.task_overdue" in inbox(world, world.alice)
    act(world, world.alice, tasks.decide_task, task, decision="approved")
    assert instance_of(world, instance_id).outcome == "approved"


def test_sla_expire_follows_the_expired_transition_and_late_jobs_are_ignored(world):
    instance_id = start(world, "expiring")
    fill(world, instance_id)
    due_now("flows.task_due")
    run_pending_jobs()
    instance = instance_of(world, instance_id)
    assert (instance.status, instance.outcome) == ("completed", "dropped")
    assert open_tasks(world, instance_id) == [] and "task_overdue" in kinds(world, instance_id)

    other = start(world, "expiring", note=world.shared)
    fill(world, other)
    [task] = open_tasks(world, other, "review")
    act(world, world.bob, tasks.decide_task, task, decision="approved")
    due_now("flows.task_due")
    run_pending_jobs()  # The task is closed: the due job does nothing.
    assert instance_of(world, other).outcome == "approved"


def test_notification_opens_the_task_only_for_who_may_still_read_it(world):
    from skeleton.flows.notifications import task_url

    instance_id = start(world, "queue")
    fill(world, instance_id)
    run_pending_jobs()
    [task] = open_tasks(world, instance_id, "review")
    with actor_tenant(world.bob, world.a):
        row = NotificationRecipient.objects.get(user=world.bob)
        assert row.payload["task"] == str(task.pk) and "audience" not in str(row.payload)
        assert task_url(world.bob, row) is None or str(task.pk) in task_url(world.bob, row)
        assert policies.visible_tasks(world.bob).filter(pk=task.pk).exists()
    with actor_tenant(world.alice, world.a):
        assert not policies.visible_tasks(world.alice).filter(pk=task.pk).exists()
        assert task_url(world.alice, row) is None
