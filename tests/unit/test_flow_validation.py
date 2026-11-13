"""Structural validation and the registry check skeleton.E029 (#201).

Each case mutates the valid sample flow (``tools/flow_fixtures.py``) in one way and expects
the problem that names it.
"""

import dataclasses

import pytest

from skeleton.flows import registry as flows
from skeleton.flows.validation import reference_problems, structural_problems
from tools.flow_fixtures import KEY, OWNER, PREPARE, review_spec, review_steps, review_transitions


def mutated(change):
    steps, transitions = review_steps(), review_transitions()
    overrides = change(steps, transitions) or {}
    return review_spec(steps=steps, transitions=transitions, **overrides)


def replace_step(steps, key, **changes):
    steps[key] = dataclasses.replace(steps[key], **changes)


def drop(transitions, source, target, on=""):
    kept = [item for item in transitions if (item.source, item.target, item.on) != (source, target, on)]
    assert len(kept) == len(transitions) - 1
    transitions[:] = kept


def retarget(transitions, source, target, new_target, on=""):
    for index, item in enumerate(transitions):
        if (item.source, item.target, item.on) == (source, target, on):
            transitions[index] = dataclasses.replace(item, target=new_target)
            return
    raise AssertionError("transition not found")


def second_start(steps, transitions):
    steps["again"] = flows.start("again")
    transitions.append(flows.Transition("again", "prepare"))


def no_start(steps, transitions):
    del steps["start"]
    drop(transitions, "start", "prepare")


def unreachable(steps, transitions):
    steps["island"] = flows.timer("island", delay_seconds=1)
    transitions.append(flows.Transition("island", "published"))


def dead_end(steps, transitions):
    steps["loop_a"] = flows.timer("loop_a", delay_seconds=1)
    steps["loop_b"] = flows.timer("loop_b", delay_seconds=1)
    transitions.append(flows.Transition("prepare", "loop_a", key="lost"))
    transitions.append(flows.Transition("loop_a", "loop_b"))
    transitions.append(flows.Transition("loop_b", "loop_a"))
    transitions[:] = [item for item in transitions if item.source != "loop_b"] + [flows.Transition("loop_b", "loop_a")]


def branch_escapes_to_end(steps, transitions):
    retarget(transitions, "legal", "joined", "withdrawn", on="rejected")


def branch_returns_to_fork(steps, transitions):
    retarget(transitions, "legal", "joined", "fork", on="rejected")


def join_entered_from_outside(steps, transitions):
    retarget(transitions, "single", "publish", "joined", on="approved")


def block_entered_from_outside(steps, transitions):
    retarget(transitions, "single", "publish", "editor", on="approved")


def join_shared_by_two_forks(steps, transitions):
    replace_step(steps, "extras", join="joined")


def nested_fork_joins_outside(steps, transitions):
    # legal -> inner fork whose join is the outer join's successor chain: must join inside the block.
    steps["inner"] = flows.parallel("inner", join="extras_done")
    retarget(transitions, "legal", "joined", "inner", on="approved")
    transitions.append(flows.Transition("inner", "joined"))
    transitions.append(flows.Transition("inner", "editor"))


CASES = [
    (lambda s, t: {"key": "Review"}, "key must look like"),
    (lambda s, t: {"version": 0}, "version must be a positive integer"),
    (lambda s, t: {"label": " "}, "label is required"),
    (lambda s, t: {"start_action": "delete"}, "start_action must be read or update"),
    (second_start, "exactly one start (found 2)"),
    (no_start, "exactly one start (found 0)"),
    (lambda s, t: t.append(flows.Transition("prepare", "nowhere", key="x")), "unknown source or target step"),
    (lambda s, t: replace_step(s, "cool", kind="pause"), "unknown step kind"),
    (lambda s, t: s.update(Bad=flows.timer("Bad", delay_seconds=1)), "invalid step key"),
    (unreachable, "step island: unreachable from the start"),
    (dead_end, "dead end (no path to an end)"),
    (lambda s, t: drop(t, "route", "single"), "at least two outgoing transitions"),
    (
        lambda s, t: t.__setitem__(4, dataclasses.replace(t[4], default=False, condition=flows.truthy("notify"))),
        "exactly one default transition",
    ),
    (lambda s, t: t.__setitem__(3, dataclasses.replace(t[3], condition=None)), "needs a condition"),
    (lambda s, t: replace_step(s, "fork", join="nowhere"), "fork without a join"),
    (lambda s, t: replace_step(s, "fork", join="verdict"), "fork without a join"),
    (branch_escapes_to_end, "reaches the end withdrawn without passing the join"),
    (branch_returns_to_fork, "returns to the fork without passing the join"),
    (join_entered_from_outside, "is entered from outside the block (single)"),
    (block_entered_from_outside, "step editor of the block is entered from outside (single)"),
    (join_shared_by_two_forks, "a join belongs to exactly one fork"),
    (nested_fork_joins_outside, "must join inside the block"),
    (lambda s, t: drop(t, "single", "prepare", on="rejected"), "needs the approved and the rejected transitions"),
    (lambda s, t: replace_step(s, "single", policy="majority"), "unknown approval policy"),
    (lambda s, t: replace_step(s, "single", policy="quorum"), "needs quorum >= 1"),
    (lambda s, t: replace_step(s, "single", assignees=()), "needs assignees"),
    (lambda s, t: replace_step(s, "editor", assignees=(flows.Assignment.field("audience"),)), "declared user field"),
    (lambda s, t: replace_step(s, "single", assignees=(flows.Assignment("crowd"),)), "unknown assignment"),
    (lambda s, t: replace_step(s, "single", assignees=(flows.Assignment.role("bad code!"),)), "invalid role code"),
    (
        lambda s, t: replace_step(s, "single", sla=flows.Sla(due_seconds=3600, on_overdue="expire")),
        "expired transition exists exactly when",
    ),
    (
        lambda s, t: replace_step(s, "single", sla=flows.Sla(due_seconds=3600, on_overdue="reassign")),
        "needs escalate_to",
    ),
    (
        lambda s, t: replace_step(s, "single", sla=flows.Sla(due_seconds=3600, remind_before_seconds=(7200,))),
        "reminders (at most five) come before the due time",
    ),
    (lambda s, t: replace_step(s, "publish", handler=""), "a service needs a handler"),
    (lambda s, t: t.append(flows.Transition("publish", "withdrawn")), "one transition per result"),
    (lambda s, t: drop(t, "confirm", "withdrawn", on="timeout"), "with a timeout, the timeout one"),
    (lambda s, t: replace_step(s, "confirm", signal=""), "a wait needs a signal name"),
    (lambda s, t: t.append(flows.Transition("cool", "verdict")), "a timer has exactly one plain outgoing"),
    (lambda s, t: t.append(flows.Transition("published", "prepare")), "an end has no outgoing transition"),
    (lambda s, t: t.append(flows.Transition("prepare", "start", key="back")), "the start has no incoming"),
    (
        lambda s, t: (
            t.__setitem__(1, dataclasses.replace(t[1], key="")) or t.__setitem__(2, dataclasses.replace(t[2], key=""))
        ),
        "each transition needs a key",
    ),
    (lambda s, t: t.__setitem__(2, dataclasses.replace(t[2], key="submit")), "duplicate transition key"),
    (
        lambda s, t: t.__setitem__(3, dataclasses.replace(t[3], condition=flows.eq("nonexistent", 1))),
        "undeclared field 'nonexistent'",
    ),
    (
        lambda s, t: t.__setitem__(3, dataclasses.replace(t[3], condition=flows.Expr("exec", name="os.system"))),
        "unknown condition operator",
    ),
    (
        lambda s, t: replace_step(s, "prepare", form=flows.Form(*PREPARE.fields, flows.Field("reason", "int"))),
        "field reason: type, sensitivity, personal and choices must agree",
    ),
    (
        lambda s, t: replace_step(s, "prepare", form=flows.Form(flows.Field("when", "moment"))),
        "unknown field type 'moment'",
    ),
    (
        lambda s, t: replace_step(s, "prepare", form=flows.Form(flows.Field("kind", "choice"))),
        "declares unique choices",
    ),
    (lambda s, t: {"subject_events": (("archived", "example_notes.x"),)}, "unknown moment"),
]


@pytest.mark.parametrize("change,problem", CASES)
def test_each_structural_rule_is_reported(change, problem):
    found = structural_problems(mutated(change))
    assert any(problem in item for item in found), found


def test_a_loop_back_to_an_earlier_step_is_valid():
    # single --rejected--> prepare and verdict --default--> prepare are returns, not dead ends.
    assert structural_problems(review_spec()) == []


@pytest.fixture
def registries(monkeypatch):
    from skeleton.flows import registry

    for name in ("_flows", "_handlers", "_conditions", "_assignments"):
        monkeypatch.setattr(registry, name, {})
    monkeypatch.setattr(registry, "_duplicates", [])
    return registry


def owners(**names):
    return {name.replace("__", "."): owner for name, owner in names.items()}


def test_references_must_be_registered_by_the_flows_own_app(monkeypatch):
    monkeypatch.setattr("skeleton.flows.validation._subject_problems", lambda spec: [])
    spec = mutated(
        lambda s, t: (
            replace_step(s, "single", assignees=(flows.Assignment.rule("example_notes.pick"),))
            or t.__setitem__(3, dataclasses.replace(t[3], condition=flows.call("example_notes.is_public")))
        )
    )
    assert structural_problems(spec) == []
    handlers = {"example_notes.announce": OWNER, "example_notes.publish": OWNER}
    found = reference_problems(spec, handlers={}, conditions={}, assignments={})
    assert sorted(found) == [
        "assignment rule 'example_notes.pick' is not registered",
        "condition 'example_notes.is_public' is not registered",
        "handler 'example_notes.announce' is not registered",
        "handler 'example_notes.publish' is not registered",
    ]
    found = reference_problems(
        spec,
        handlers={**handlers, "example_notes.publish": "other_app"},
        conditions={"example_notes.is_public": OWNER},
        assignments={"example_notes.pick": "other_app"},
    )
    assert sorted(found) == [
        "assignment rule 'example_notes.pick' belongs to another app",
        "handler 'example_notes.publish' belongs to another app",
    ]
    assert (
        reference_problems(
            spec,
            handlers=handlers,
            conditions={"example_notes.is_public": OWNER},
            assignments={"example_notes.pick": OWNER},
        )
        == []
    )


def test_registry_reports_duplicates_and_structural_problems(registries, monkeypatch):
    monkeypatch.setattr("skeleton.flows.validation._subject_problems", lambda spec: [])
    flows.register_flow_handler("example_notes.announce", lambda context: None, owner_app=OWNER)
    flows.register_flow_handler("example_notes.publish", lambda context: None, owner_app=OWNER)
    flows.register_flow(review_spec())
    assert flows.flow_problems() == []
    assert flows.register_flow(review_spec()) is flows.get_flow(KEY)  # The same spec twice is fine.
    assert flows.flow_problems() == []
    flows.register_flow(review_spec(label="Outra"))
    flows.register_flow_handler("example_notes.publish", lambda context: "x", owner_app=OWNER)
    assert flows.flow_problems() == [
        f"flow {KEY}: duplicate registration",
        "handler example_notes.publish: duplicate registration",
    ]


def test_registry_reports_a_broken_flow_under_its_key(registries):
    flows.register_flow(mutated(unreachable))
    assert flows.flow_problems() == [f"{KEY}: step island: unreachable from the start"]
    assert [flow.key for flow in flows.all_flows()] == [KEY]
