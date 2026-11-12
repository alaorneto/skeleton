"""Real concurrency of the flow engine on PostgreSQL (#203, #204): separate connections and threads.

The instance row lock serializes the commands of one instance: parallel branches that finish
together fire the join once, two people on one task have one winner, and two starts about
one record create one instance.
"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import connection, connections

from skeleton.access.models import AccessGroup, GroupMember
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.flows import registry as flows
from skeleton.flows import services, tasks
from skeleton.flows.errors import FlowConflict
from skeleton.flows.models import FlowInstance, FlowStepRun, FlowTask
from skeleton.tenancy.context import system_context
from tools.integration_database import disposable_database_pair
from tools.security_fixtures import actor_tenant, build_security_fixture

pytestmark = pytest.mark.needs_example_app

OWNER = "example_notes"
board = flows.Assignment.group("board")


def spec(key, steps, transitions):
    return flows.FlowSpec(
        key=f"example_notes.{key}",
        version=1,
        label=key,
        owner_app=OWNER,
        subject_table="example_notes.note",
        steps=(flows.start("start"), *steps, flows.end("done", outcome="done")),
        transitions=transitions,
    )


PARALLEL = spec(
    "race_parallel",
    (
        flows.parallel("fork", join="joined"),
        flows.approval("first", assignees=(flows.Assignment.starter(),)),
        flows.approval("second", assignees=(flows.Assignment.group("board", expand=True),), policy="all"),
        flows.join("joined"),
    ),
    (
        flows.Transition("start", "fork"),
        flows.Transition("fork", "first"),
        flows.Transition("fork", "second"),
        flows.Transition("first", "joined", on="approved"),
        flows.Transition("first", "joined", on="rejected"),
        flows.Transition("second", "joined", on="approved"),
        flows.Transition("second", "joined", on="rejected"),
        flows.Transition("joined", "done"),
    ),
)
QUEUE = spec(
    "race_queue",
    (flows.approval("review", assignees=(board,)),),
    (
        flows.Transition("start", "review"),
        flows.Transition("review", "done", on="approved"),
        flows.Transition("review", "start_again", on="rejected"),
    ),
)


@pytest.fixture
def world(skeleton_db, monkeypatch):
    from skeleton.flows import registry

    disposable_database_pair()
    assert connection.vendor == "postgresql"
    for name in ("_flows", "_handlers", "_conditions", "_assignments"):
        monkeypatch.setattr(registry, name, {})
    monkeypatch.setattr(registry, "_duplicates", [])
    flows.register_flow(PARALLEL)
    queue = flows.FlowSpec(
        **{
            **{name: getattr(QUEUE, name) for name in ("key", "version", "label", "owner_app", "subject_table")},
            "steps": (*QUEUE.steps, flows.end("start_again", outcome="again")),
            "transitions": QUEUE.transitions,
        }
    )
    flows.register_flow(queue)
    assert flows.flow_problems() == []
    fixture = build_security_fixture()
    with system_context(reason="test:flow-race", organization_id=fixture.a.pk):
        group = AccessGroup(organization=fixture.a, code="board", name="Diretoria")
        group.save()
        for membership in (fixture.alice_membership, fixture.bob_membership):
            GroupMember(organization=fixture.a, group=group, membership=membership).save()
    return fixture


def together(calls):
    """Run the calls at the same moment, each on its own connection; exceptions are returned."""
    barrier = Barrier(len(calls))

    def run(call):
        connections.close_all()
        try:
            return call(barrier)
        except Exception as error:  # noqa: BLE001 - the outcome under test
            return error
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return list(pool.map(run, calls, timeout=60))


def start(world, key, note):
    with actor_tenant(world.alice, world.a):
        instance, _ = services.start_instance(f"example_notes.{key}", subject_id=note.pk, actor=world.alice)
        return instance.pk


def test_parallel_branches_finishing_together_fire_the_join_once(world):
    instance_id = start(world, "race_parallel", world.private)
    with actor_tenant(world.admin, world.a):
        open_tasks = list(FlowTask.objects.filter(instance_id=instance_id, status="open"))
    assert len(open_tasks) == 3  # Alice in the first branch; Alice and Bob in the second.
    users = {world.alice.pk: world.alice, world.bob.pk: world.bob}

    def decide(task):
        def call(barrier):
            user = users[task.assignee_id]
            with actor_tenant(user, world.a):
                barrier.wait(10)
                return tasks.decide_task(task.pk, actor=user, expected_revision=task.revision, decision="approved")

        return call

    results = together([decide(task) for task in open_tasks])
    assert not [item for item in results if isinstance(item, Exception)], results
    with actor_tenant(world.admin, world.a):
        instance = FlowInstance.objects.get(pk=instance_id)
        assert (instance.status, instance.outcome) == ("completed", "done")
        runs = list(FlowStepRun.objects.filter(instance=instance).order_by("sequence"))
        assert [run.status for run in runs if run.step_key == "joined"] == ["completed"]  # Exactly one join run.
        assert [run.step_key for run in runs].count("done") == 1
        assert sorted(run.sequence for run in runs) == list(range(1, len(runs) + 1))  # No lost or repeated sequence.
        assert instance.run_count == len(runs)


def test_two_people_on_the_same_queue_task_have_one_winner(world):
    instance_id = start(world, "race_queue", world.private)
    with actor_tenant(world.admin, world.a):
        task = FlowTask.objects.get(instance_id=instance_id, status="open")

    def decide(user, decision):
        def call(barrier):
            with actor_tenant(user, world.a):
                barrier.wait(10)
                return tasks.decide_task(task.pk, actor=user, expected_revision=task.revision, decision=decision)

        return call

    results = together([decide(world.alice, "approved"), decide(world.bob, "rejected")])
    winners = [item for item in results if not isinstance(item, Exception)]
    losers = [item for item in results if isinstance(item, Exception)]
    # The loser read the same revision: a conflict, never "not found" and never a second write.
    assert len(winners) == 1 and len(losers) == 1, results
    assert isinstance(losers[0], ConcurrentUpdate), results
    with actor_tenant(world.admin, world.a):
        stored = FlowTask.objects.get(pk=task.pk)
        instance = FlowInstance.objects.get(pk=instance_id)
        assert stored.decision == winners[0].decision and stored.completed_by_id == winners[0].completed_by_id
        assert instance.outcome == ("done" if stored.decision == "approved" else "again")


def test_two_starts_about_one_record_create_one_instance(world):
    def begin(key):
        def call(barrier):
            with actor_tenant(world.alice, world.a):
                barrier.wait(10)
                return services.start_instance(
                    "example_notes.race_queue", subject_id=world.shared.pk, actor=world.alice, idempotency_key=key
                )[0].pk

        return call

    results = together([begin("a"), begin("b"), begin(None)])
    created = [item for item in results if not isinstance(item, Exception)]
    refused = [item for item in results if isinstance(item, Exception)]
    assert len(created) == 1 and len(refused) == 2, results
    assert all(isinstance(item, FlowConflict) and item.code == "already_running" for item in refused)
    with actor_tenant(world.admin, world.a):
        assert FlowInstance.objects.filter(subject_sysid=world.shared.pk).count() == 1
    # The same idempotency key from two callers: both get the one instance.
    same = together([begin("same"), begin("same")])
    assert all(isinstance(item, FlowConflict) for item in same)  # One is already running for this record.
