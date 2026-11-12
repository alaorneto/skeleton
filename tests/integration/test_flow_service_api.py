"""FlowService through the contract kit (#205): PostgreSQL with the runtime role.

Each public method is proven on the authorized path, default deny, A/B isolation, disabled
app, divergent actor and, for commands, CAS and idempotency.
"""

from uuid import uuid4

import pytest
from django.db import transaction

from examples.notes.models import Note
from skeleton.flows import registry as flows
from skeleton.service_api import Conflict, Forbidden, InvalidInput, NotFound, Page
from skeleton.service_api.flows import FlowService, InstanceInfo, StartReceipt, TaskDetail, TaskSummary
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.service_api import (
    assert_cas_conflict,
    assert_denied_as_not_found,
    assert_rejects_other_actor,
    assert_service_contract,
    assert_tenant_isolated,
    assert_unavailable_when_disabled,
    build_service_scenario,
)

OWNER = "example_notes"
KEY = "example_notes.approval"
SECRET = "valor reservado 4242"
FORM = flows.Form(
    flows.Field("summary", "str", label="Resumo", required=True, max_length=100),
    flows.Field("remark", "text", label="Reservado", sensitive=True),
)
service = FlowService()


def approval_spec():
    return flows.FlowSpec(
        key=KEY,
        version=1,
        label="Aprovação de nota",
        owner_app=OWNER,
        subject_table="example_notes.note",
        steps=(
            flows.start("start"),
            flows.task("fill", label="Preencher", assignees=(flows.Assignment.starter(),), form=FORM),
            flows.approval("review", label="Aprovar", assignees=(flows.Assignment.role("organization_admin"),)),
            flows.wait("confirm", signal="confirmed"),
            flows.end("done", outcome="approved"),
        ),
        transitions=(
            flows.Transition("start", "fill"),
            flows.Transition("fill", "review", key="submit", label="Enviar"),
            flows.Transition("review", "confirm", on="approved"),
            flows.Transition("review", "fill", on="rejected"),
            flows.Transition("confirm", "done", on="received"),
        ),
    )


def as_app(function, module="examples.notes.services"):
    """Call from a frame of the app's module: how the owning app calls the service."""
    namespace = {"__name__": module, "function": function}
    exec("def call(*args, **kwargs):\n    return function(*args, **kwargs)\n", namespace)  # noqa: S102
    return namespace["call"]


@pytest.fixture
def scenario(skeleton_db, monkeypatch):
    from skeleton.flows import registry

    for name in ("_flows", "_handlers", "_conditions", "_assignments"):
        monkeypatch.setattr(registry, name, {})
    monkeypatch.setattr(registry, "_duplicates", [])
    flows.register_flow(approval_spec())
    assert flows.flow_problems() == []
    built = build_service_scenario(Note, slug="flow-service")
    built.note = built.create(built.a, code="own", title="Own")
    built.other = built.create(built.a, code="other", title="Other")
    built.foreign = built.create(built.b, code="foreign-code", title="Foreign title")
    return built


def start(scenario, note=None, organization=None, **kwargs):
    note = note or scenario.note
    return scenario.caller(
        lambda actor: as_app(service.start)(actor=actor, flow_key=KEY, subject_id=note.pk, **kwargs),
        organization=organization,
    )()


def call(scenario, function, user=None, organization=None):
    return scenario.caller(function, user=user, organization=organization)()


def my_task(scenario, user=None, scope="mine", organization=None):
    page = call(scenario, lambda actor: service.tasks(actor=actor, scope=scope), user=user, organization=organization)
    return page.items[0]


def fill(scenario, **data):
    task = my_task(scenario)
    data.setdefault("summary", "Resumo")
    return call(
        scenario,
        lambda actor: service.complete_task(
            actor=actor, task_id=task.sysid, expected_revision=task.revision, data=tuple(data.items())
        ),
    )


def test_flow_service_passes_the_static_contract():
    assert_service_contract(FlowService)


def test_authorized_path_from_start_to_the_end(scenario):
    receipt = start(scenario, idempotency_key="k-1")
    assert isinstance(receipt, StartReceipt) and receipt.created and receipt.status == "running"
    assert start(scenario, idempotency_key="k-1") == StartReceipt(
        receipt.sysid, receipt.number, "running", receipt.revision, False
    )
    info = call(scenario, lambda actor: service.instance(actor=actor, instance_id=receipt.sysid))
    assert isinstance(info, InstanceInfo) and info.subject_id == scenario.note.pk and info.label == "Aprovação de nota"
    assert [(step.key, step.status) for step in info.active_steps] == [("fill", "active")]
    assert info.created_at.utcoffset().total_seconds() == 0
    assert call(scenario, lambda actor: service.open_task_count(actor=actor)) == 1

    task = my_task(scenario)
    assert isinstance(task, TaskSummary) and (task.step_label, task.kind, task.queue) == ("Preencher", "task", "")
    detail = call(scenario, lambda actor: service.task(actor=actor, task_id=task.sysid))
    assert isinstance(detail, TaskDetail) and detail.can_act and not detail.can_claim and detail.can_reassign
    assert [(field.name, field.required, field.sensitive) for field in detail.fields] == [
        ("summary", True, False),
        ("remark", False, True),
    ]
    assert [action.key for action in detail.actions] == ["submit"]
    done = fill(scenario, summary="Tudo certo", remark=SECRET)
    assert done.status == "completed"

    info = call(scenario, lambda actor: service.instance(actor=actor, instance_id=receipt.sysid))
    assert [(item.name, item.value) for item in info.data] == [("summary", "Tudo certo")]
    assert SECRET not in repr(info)
    queued = my_task(scenario, user=scenario.admin_a, scope="available")
    assert (queued.queue, queued.assignee_id, queued.kind) == ("role", None, "approval")
    seen = call(scenario, lambda actor: service.task(actor=actor, task_id=queued.sysid), user=scenario.admin_a)
    assert seen.can_claim and seen.actions == ()
    claimed = call(
        scenario,
        lambda actor: service.claim_task(actor=actor, task_id=queued.sysid, expected_revision=queued.revision),
        user=scenario.admin_a,
    )
    released = call(
        scenario,
        lambda actor: service.release_task(actor=actor, task_id=queued.sysid, expected_revision=claimed.revision),
        user=scenario.admin_a,
    )
    decided = call(
        scenario,
        lambda actor: service.decide_task(
            actor=actor, task_id=queued.sysid, expected_revision=released.revision, decision="approved", comment="ok"
        ),
        user=scenario.admin_a,
    )
    assert (decided.decision, decided.assignee_id) == ("approved", scenario.admin_a.pk)
    delivered = scenario.caller(
        lambda actor: as_app(service.signal)(actor=actor, instance_id=receipt.sysid, name="confirmed")
    )
    assert delivered().delivered is True and delivered().delivered is False
    info = call(scenario, lambda actor: service.instance(actor=actor, instance_id=receipt.sysid))
    assert (info.status, info.outcome, info.active_steps) == ("completed", "approved", ())

    history = call(scenario, lambda actor: service.history(actor=actor, instance_id=receipt.sysid, page_size=100))
    assert isinstance(history, Page) and [entry.sequence for entry in history.items] == sorted(
        entry.sequence for entry in history.items
    )
    kinds = [entry.kind for entry in history.items]
    assert kinds[0] == "instance_started" and kinds[-1] == "instance_completed" and "task_decided" in kinds
    decision = next(entry for entry in history.items if entry.kind == "task_decided")
    assert (decision.actor_id, decision.comment, decision.step_label) == (scenario.admin_a.pk, "ok", "Aprovar")
    assert SECRET not in repr(history)
    listed = call(scenario, lambda actor: service.instances(actor=actor, status="completed"))
    assert [item.sysid for item in listed.items] == [receipt.sysid]
    everything = call(scenario, lambda actor: service.tasks(actor=actor, scope="all", status="completed"))
    assert {item.step_key for item in everything.items} == {"fill"}  # The member's own past task only.


def test_sensitive_values_reach_only_who_can_act_on_the_task(scenario):
    start(scenario)
    fill(scenario, remark=SECRET)
    review = my_task(scenario, user=scenario.admin_a, scope="available")
    call(
        scenario,
        lambda actor: service.decide_task(
            actor=actor, task_id=review.sysid, expected_revision=review.revision, decision="rejected"
        ),
        user=scenario.admin_a,
    )
    again = my_task(scenario)  # Back to the member, who may act: the stored secret pre-fills the form.
    mine = call(scenario, lambda actor: service.task(actor=actor, task_id=again.sysid))
    assert {field.name: field.value for field in mine.fields}["remark"] == SECRET
    admin_view = call(scenario, lambda actor: service.task(actor=actor, task_id=again.sysid), user=scenario.admin_a)
    assert not admin_view.can_act and {field.name: field.value for field in admin_view.fields}["remark"] is None
    assert SECRET not in repr(admin_view)


def test_only_the_owning_app_starts_and_signals(scenario):
    with pytest.raises(Forbidden):
        call(scenario, lambda actor: service.start(actor=actor, flow_key=KEY, subject_id=scenario.note.pk))
    other_app = as_app(service.start, module="skeleton.contrib.calendar.services")
    with pytest.raises(Forbidden):
        call(scenario, lambda actor: other_app(actor=actor, flow_key=KEY, subject_id=scenario.note.pk))
    with pytest.raises(InvalidInput):
        call(scenario, lambda actor: as_app(service.start)(actor=actor, flow_key="x.y", subject_id=scenario.note.pk))
    receipt = start(scenario)
    with pytest.raises(Forbidden):
        call(scenario, lambda actor: service.signal(actor=actor, instance_id=receipt.sysid, name="confirmed"))


def test_default_deny(scenario):
    receipt = start(scenario)
    task = my_task(scenario)
    denied = scenario.denied  # Member of A without any table permission, taking no part in the flow.
    assert_denied_as_not_found(
        scenario.caller(lambda actor: service.instance(actor=actor, instance_id=receipt.sysid), user=denied),
        missing=scenario.caller(lambda actor: service.instance(actor=actor, instance_id=uuid4()), user=denied),
    )
    assert_denied_as_not_found(
        scenario.caller(lambda actor: service.history(actor=actor, instance_id=receipt.sysid), user=denied)
    )
    assert_denied_as_not_found(
        scenario.caller(lambda actor: service.task(actor=actor, task_id=task.sysid), user=denied),
        missing=scenario.caller(lambda actor: service.task(actor=actor, task_id=uuid4()), user=denied),
    )
    commands = (
        lambda actor: service.claim_task(actor=actor, task_id=task.sysid, expected_revision=task.revision),
        lambda actor: service.release_task(actor=actor, task_id=task.sysid, expected_revision=task.revision),
        lambda actor: service.reassign_task(
            actor=actor, task_id=task.sysid, expected_revision=task.revision, to_user_id=actor.pk
        ),
        lambda actor: service.complete_task(actor=actor, task_id=task.sysid, expected_revision=task.revision),
        lambda actor: service.decide_task(
            actor=actor, task_id=task.sysid, expected_revision=task.revision, decision="approved"
        ),
        lambda actor: service.cancel(actor=actor, instance_id=receipt.sysid, expected_revision=receipt.revision),
        lambda actor: service.suspend(actor=actor, instance_id=receipt.sysid, expected_revision=receipt.revision),
        lambda actor: service.resume(actor=actor, instance_id=receipt.sysid, expected_revision=receipt.revision),
        lambda actor: service.retry(actor=actor, instance_id=receipt.sysid, expected_revision=receipt.revision),
        lambda actor: as_app(service.signal)(actor=actor, instance_id=receipt.sysid, name="confirmed"),
        lambda actor: as_app(service.start)(actor=actor, flow_key=KEY, subject_id=scenario.other.pk),
    )
    for command in commands:
        assert_denied_as_not_found(scenario.caller(command, user=denied))
    for query in (
        lambda actor: service.instances(actor=actor).items,
        lambda actor: service.tasks(actor=actor).items,
        lambda actor: service.tasks(actor=actor, scope="available").items,
        lambda actor: service.tasks(actor=actor, scope="all").items,
    ):
        assert call(scenario, query, user=denied) == ()
    assert call(scenario, lambda actor: service.open_task_count(actor=actor), user=denied) == 0
    # The member reads the instance but does not administer it; the review is not the member's.
    info = call(scenario, lambda actor: service.instance(actor=actor, instance_id=receipt.sysid))
    for forbidden in (
        lambda actor: service.suspend(actor=actor, instance_id=receipt.sysid, expected_revision=info.revision),
        lambda actor: service.resume(actor=actor, instance_id=receipt.sysid, expected_revision=info.revision),
        lambda actor: service.retry(actor=actor, instance_id=receipt.sysid, expected_revision=info.revision),
        lambda actor: service.claim_task(actor=actor, task_id=task.sysid, expected_revision=task.revision + 9),
    ):
        with pytest.raises((Forbidden, Conflict)):
            call(scenario, forbidden)
    with pytest.raises(Forbidden):
        call(
            scenario,
            lambda actor: service.suspend(actor=actor, instance_id=receipt.sysid, expected_revision=info.revision),
        )


def test_organizations_are_isolated(scenario):
    theirs = start(scenario, note=scenario.foreign, organization=scenario.b)
    their_task = my_task(scenario, organization=scenario.b)
    start(scenario)
    markers = [scenario.foreign, theirs.sysid, their_task.sysid, "foreign-code", "Foreign title"]
    reads = (
        lambda actor: service.instances(actor=actor),
        lambda actor: service.instances(actor=actor, subject_id=scenario.foreign.pk),
        lambda actor: service.tasks(actor=actor),
        lambda actor: service.tasks(actor=actor, scope="all"),
        lambda actor: service.instance(actor=actor, instance_id=theirs.sysid),
        lambda actor: service.history(actor=actor, instance_id=theirs.sysid),
        lambda actor: service.task(actor=actor, task_id=their_task.sysid),
        lambda actor: as_app(service.start)(actor=actor, flow_key=KEY, subject_id=scenario.foreign.pk),
        lambda actor: as_app(service.signal)(actor=actor, instance_id=theirs.sysid, name="confirmed"),
        lambda actor: service.complete_task(
            actor=actor, task_id=their_task.sysid, expected_revision=their_task.revision, data=(("summary", "x"),)
        ),
        lambda actor: service.cancel(actor=actor, instance_id=theirs.sysid, expected_revision=theirs.revision),
    )
    for user in (scenario.member, scenario.admin_a):
        for read in reads:
            assert_tenant_isolated(scenario.caller(read, user=user), foreign=markers)
    assert_denied_as_not_found(
        scenario.caller(lambda actor: service.instance(actor=actor, instance_id=theirs.sysid), user=scenario.admin_a),
        missing=scenario.caller(
            lambda actor: service.instance(actor=actor, instance_id=uuid4()), user=scenario.admin_a
        ),
    )
    assert call(scenario, lambda actor: service.open_task_count(actor=actor)) == 1
    assert len(call(scenario, lambda actor: service.instances(actor=actor)).items) == 1
    # The same member reads it inside B: the isolation above is the tenant.
    inside = call(
        scenario, lambda actor: service.instance(actor=actor, instance_id=theirs.sysid), organization=scenario.b
    )
    assert inside.subject_id == scenario.foreign.pk


def test_disabled_app_is_unavailable(scenario):
    receipt = start(scenario)
    task = my_task(scenario)
    start(scenario, note=scenario.foreign, organization=scenario.b)
    calls = (
        lambda actor: as_app(service.start)(actor=actor, flow_key=KEY, subject_id=scenario.other.pk),
        lambda actor: as_app(service.signal)(actor=actor, instance_id=receipt.sysid, name="confirmed"),
        lambda actor: service.instance(actor=actor, instance_id=receipt.sysid),
        lambda actor: service.history(actor=actor, instance_id=receipt.sysid),
        lambda actor: service.task(actor=actor, task_id=task.sysid),
        lambda actor: service.claim_task(actor=actor, task_id=task.sysid, expected_revision=task.revision),
        lambda actor: service.release_task(actor=actor, task_id=task.sysid, expected_revision=task.revision),
        lambda actor: service.reassign_task(
            actor=actor, task_id=task.sysid, expected_revision=task.revision, to_user_id=scenario.admin_a.pk
        ),
        lambda actor: service.complete_task(actor=actor, task_id=task.sysid, expected_revision=task.revision),
        lambda actor: service.decide_task(
            actor=actor, task_id=task.sysid, expected_revision=task.revision, decision="approved"
        ),
        lambda actor: service.cancel(actor=actor, instance_id=receipt.sysid, expected_revision=receipt.revision),
        lambda actor: service.suspend(actor=actor, instance_id=receipt.sysid, expected_revision=receipt.revision),
        lambda actor: service.resume(actor=actor, instance_id=receipt.sysid, expected_revision=receipt.revision),
        lambda actor: service.retry(actor=actor, instance_id=receipt.sysid, expected_revision=receipt.revision),
    )
    for item in calls:
        assert_unavailable_when_disabled(scenario.caller(item), disabled=scenario.application_disabled)
    with scenario.application_disabled():
        assert call(scenario, lambda actor: service.instances(actor=actor)).items == ()
        assert call(scenario, lambda actor: service.tasks(actor=actor)).items == ()
        assert call(scenario, lambda actor: service.tasks(actor=actor, scope="all")).items == ()
        assert call(scenario, lambda actor: service.open_task_count(actor=actor)) == 0
        # Disabled only in A: B keeps working.
        assert call(scenario, lambda actor: service.open_task_count(actor=actor), organization=scenario.b) == 1
    assert call(scenario, lambda actor: service.open_task_count(actor=actor)) == 1


def test_actor_must_match_the_context(scenario):
    receipt = start(scenario)
    task = my_task(scenario)
    other = scenario.admin_a
    calls = (
        lambda actor: as_app(service.start)(actor=other, flow_key=KEY, subject_id=scenario.other.pk),
        lambda actor: as_app(service.start)(actor=None, flow_key=KEY, subject_id=scenario.other.pk),
        lambda actor: as_app(service.signal)(actor=other, instance_id=receipt.sysid, name="confirmed"),
        lambda actor: service.instance(actor=other, instance_id=receipt.sysid),
        lambda actor: service.instances(actor=other),
        lambda actor: service.history(actor=other, instance_id=receipt.sysid),
        lambda actor: service.tasks(actor=other),
        lambda actor: service.open_task_count(actor=other),
        lambda actor: service.task(actor=other, task_id=task.sysid),
        lambda actor: service.claim_task(actor=other, task_id=task.sysid, expected_revision=task.revision),
        lambda actor: service.release_task(actor=other, task_id=task.sysid, expected_revision=task.revision),
        lambda actor: service.reassign_task(
            actor=other, task_id=task.sysid, expected_revision=task.revision, to_user_id=actor.pk
        ),
        lambda actor: service.complete_task(actor=other, task_id=task.sysid, expected_revision=task.revision),
        lambda actor: service.decide_task(
            actor=other, task_id=task.sysid, expected_revision=task.revision, decision="approved"
        ),
        lambda actor: service.cancel(actor=other, instance_id=receipt.sysid, expected_revision=receipt.revision),
        lambda actor: service.suspend(actor=other, instance_id=receipt.sysid, expected_revision=receipt.revision),
        lambda actor: service.resume(actor=other, instance_id=receipt.sysid, expected_revision=receipt.revision),
        lambda actor: service.retry(actor=other, instance_id=receipt.sysid, expected_revision=receipt.revision),
    )
    for item in calls:
        assert_rejects_other_actor(scenario.caller(item))


def test_task_commands_have_one_winner_per_revision(scenario):
    receipt = start(scenario)
    task = my_task(scenario)

    def complete(summary):
        return scenario.caller(
            lambda actor: service.complete_task(
                actor=actor, task_id=task.sysid, expected_revision=task.revision, data=(("summary", summary),)
            )
        )

    assert_cas_conflict(complete("first"), complete("second"))
    info = call(scenario, lambda actor: service.instance(actor=actor, instance_id=receipt.sysid))
    assert [(item.name, item.value) for item in info.data] == [("summary", "first")]

    review = my_task(scenario, user=scenario.admin_a, scope="available")

    def by_admin(function):
        return scenario.caller(function, user=scenario.admin_a)

    claimed = assert_cas_conflict(
        by_admin(
            lambda actor: service.claim_task(actor=actor, task_id=review.sysid, expected_revision=review.revision)
        ),
        by_admin(
            lambda actor: service.claim_task(actor=actor, task_id=review.sysid, expected_revision=review.revision)
        ),
    )
    moved = assert_cas_conflict(
        by_admin(
            lambda actor: service.reassign_task(
                actor=actor, task_id=review.sysid, expected_revision=claimed.revision, to_user_id=scenario.member.pk
            )
        ),
        by_admin(  # The administrator may still reassign, but that revision is gone.
            lambda actor: service.reassign_task(
                actor=actor, task_id=review.sysid, expected_revision=claimed.revision, to_user_id=scenario.denied.pk
            )
        ),
    )
    assert moved.assignee_id == scenario.member.pk

    def decide(decision):
        return scenario.caller(
            lambda actor: service.decide_task(
                actor=actor, task_id=review.sysid, expected_revision=moved.revision, decision=decision
            )
        )

    assert_cas_conflict(decide("approved"), decide("rejected"))
    info = call(scenario, lambda actor: service.instance(actor=actor, instance_id=receipt.sysid))
    assert [step.key for step in info.active_steps] == ["confirm"]


def test_instance_commands_have_one_winner_per_revision(scenario):
    receipt = start(scenario)

    def admin(function):
        return scenario.caller(function, user=scenario.admin_a)

    def revision():
        return admin(lambda actor: service.instance(actor=actor, instance_id=receipt.sysid))().revision

    seen = revision()
    suspended = assert_cas_conflict(
        admin(lambda actor: service.suspend(actor=actor, instance_id=receipt.sysid, expected_revision=seen)),
        admin(lambda actor: service.cancel(actor=actor, instance_id=receipt.sysid, expected_revision=seen)),
    )
    assert suspended.status == "suspended"
    task = my_task(scenario)
    with pytest.raises(Conflict) as frozen:
        call(
            scenario,
            lambda actor: service.complete_task(
                actor=actor, task_id=task.sysid, expected_revision=task.revision, data=(("summary", "x"),)
            ),
        )
    assert frozen.value.code == "instance_suspended"
    resumed = assert_cas_conflict(
        admin(
            lambda actor: service.resume(actor=actor, instance_id=receipt.sysid, expected_revision=suspended.revision)
        ),
        admin(
            lambda actor: service.resume(actor=actor, instance_id=receipt.sysid, expected_revision=suspended.revision)
        ),
    )
    with pytest.raises(Conflict) as healthy:
        admin(lambda actor: service.retry(actor=actor, instance_id=receipt.sysid, expected_revision=resumed.revision))()
    assert healthy.value.code == "not_failed"
    cancelled = assert_cas_conflict(  # The starter may cancel.
        scenario.caller(
            lambda actor: service.cancel(
                actor=actor, instance_id=receipt.sysid, expected_revision=resumed.revision, reason="desisti"
            )
        ),
        admin(
            lambda actor: service.suspend(actor=actor, instance_id=receipt.sysid, expected_revision=resumed.revision)
        ),
    )
    assert cancelled.status == "cancelled" and cancelled.finished_at is not None


def test_start_conflicts_and_invalid_input(scenario):
    start(scenario)
    with pytest.raises(Conflict) as running:
        start(scenario)
    assert running.value.code == "already_running"
    task = my_task(scenario)
    invalid = (
        lambda actor: service.instance(actor=actor, instance_id="not-a-uuid"),
        lambda actor: service.instances(actor=actor, status="exploded"),
        lambda actor: service.instances(actor=actor, page_size=101),
        lambda actor: service.tasks(actor=actor, scope="everyone"),
        lambda actor: service.tasks(actor=actor, scope="mine", status="completed"),
        lambda actor: service.complete_task(
            actor=actor, task_id=task.sysid, expected_revision=task.revision, data=(("summary", "x"), ("admin", True))
        ),
        lambda actor: service.complete_task(actor=actor, task_id=task.sysid, expected_revision=task.revision, data={}),
        lambda actor: service.complete_task(actor=actor, task_id=task.sysid, expected_revision="1"),
        lambda actor: service.complete_task(
            actor=actor, task_id=task.sysid, expected_revision=task.revision, data=(("summary", ""),)
        ),
        lambda actor: service.reassign_task(
            actor=actor, task_id=task.sysid, expected_revision=task.revision, to_user_id=scenario.outsider.pk
        ),
    )
    for item in invalid:
        with pytest.raises(InvalidInput):
            call(scenario, item)
    with pytest.raises(Conflict) as wrong_kind:
        call(
            scenario,
            lambda actor: service.decide_task(
                actor=actor, task_id=task.sysid, expected_revision=task.revision, decision="approved"
            ),
        )
    assert wrong_kind.value.code == "not_an_approval"


def test_caller_rollback_leaves_no_instance_task_or_job(scenario):
    from skeleton.jobs.models import Job

    before = Job.all_objects.count()
    with scenario.as_member() as actor:
        with transaction.atomic():
            as_app(service.start)(actor=actor, flow_key=KEY, subject_id=scenario.note.pk)
            assert service.open_task_count(actor=actor) == 1
            transaction.set_rollback(True)
        assert service.open_task_count(actor=actor) == 0
        assert service.instances(actor=actor).items == ()
    assert Job.all_objects.count() == before
    assert run_pending_jobs() == []
    with pytest.raises(NotFound):
        call(scenario, lambda actor: service.instance(actor=actor, instance_id=uuid4()))
