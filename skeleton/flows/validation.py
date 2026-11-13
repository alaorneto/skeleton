"""Structural validation of a flow specification (docs/flows.md, "Validação estrutural").

``structural_problems`` needs nothing but the specification: keys, the graph (reachability,
dead ends), the rules of each step kind, block pairing of forks and joins and the coherence
of the declared form fields. ``reference_problems`` checks the names the specification
refers to against the registries declared in code. Neither queries the database.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from . import expressions
from .spec import (
    ASSIGNMENT_KINDS,
    FIELD_TYPES,
    HUMAN_KINDS,
    MOMENTS,
    OVERDUE_ACTIONS,
    POLICIES,
    SPLIT_KINDS,
    STEP_KINDS,
    Assignment,
    FlowSpec,
    Form,
    Step,
    Transition,
)

FLOW_KEY = re.compile(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_.]*")
NAME = re.compile(r"[a-z][a-z0-9_]*")
EVENT_CODE = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+")
CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]*")
MAX_STEPS = 200
MAX_FIELDS = 100
RESULTS = {
    "task": ("expired",),
    "approval": ("approved", "rejected", "expired"),
    "wait": ("received", "timeout"),
}


def _reach(starts: Iterable[str], edges: Mapping[str, list[str]], *, stop: frozenset[str] = frozenset()) -> set[str]:
    """Every step reachable from ``starts`` (inclusive); steps in ``stop`` are reached but not expanded."""
    seen: set[str] = set()
    pending = list(starts)
    while pending:
        key = pending.pop()
        if key in seen:
            continue
        seen.add(key)
        if key not in stop:
            pending.extend(edges.get(key, ()))
    return seen


MAX_RETENTION_DAYS = 36500


def _form_problems(where: str, form: Any) -> list[str]:
    if not isinstance(form, Form):
        return [f"{where}: form must be a Form"]
    problems: list[str] = []
    names = form.names()
    if not names or len(names) > MAX_FIELDS:
        problems.append(f"{where}: a form declares between 1 and {MAX_FIELDS} fields")
    if len(set(names)) != len(names):
        problems.append(f"{where}: duplicate field name")
    for item in form.fields:
        label = f"{where}.{item.name}"
        if not isinstance(item.name, str) or not NAME.fullmatch(item.name) or len(item.name) > 64:
            problems.append(f"{label}: invalid field name")
        if item.type not in FIELD_TYPES:
            problems.append(f"{label}: unknown field type {item.type!r}")
            continue
        if item.type == "choice":
            if not item.choices or len(set(item.choices)) != len(item.choices):
                problems.append(f"{label}: a choice field declares unique choices")
            if any(not isinstance(choice, str) or not choice or len(choice) > 100 for choice in item.choices):
                problems.append(f"{label}: choices are short nonempty texts")
        elif item.choices:
            problems.append(f"{label}: only choice fields declare choices")
        if type(item.max_length) is not int or item.max_length < 0 or item.max_length > 20000:
            problems.append(f"{label}: invalid max_length")
        if type(item.personal) is not bool:
            problems.append(f"{label}: personal is true or false")
        if item.max_length and item.type not in ("str", "text"):
            problems.append(f"{label}: only text fields declare max_length")
        for bound in (item.minimum, item.maximum):
            if bound == "":
                continue
            if item.type not in ("int", "decimal") or not re.fullmatch(r"-?[0-9]+(\.[0-9]+)?", str(bound)):
                problems.append(f"{label}: minimum and maximum are numbers of int or decimal fields")
    return problems


def _field_coherence(spec: FlowSpec) -> list[str]:
    problems: list[str] = []
    seen: dict[str, Any] = {}
    for form in spec.forms():
        if not isinstance(form, Form):
            continue
        for item in form.fields:
            shape = (item.type, item.sensitive, item.personal, tuple(item.choices))
            if seen.setdefault(item.name, shape) != shape:
                problems.append(f"field {item.name}: type, sensitivity, personal and choices must agree in every form")
    return sorted(set(problems))


def _assignment_problems(where: str, items: Any, spec: FlowSpec) -> list[str]:
    problems: list[str] = []
    fields = spec.data_fields()
    for item in items:
        if not isinstance(item, Assignment) or item.kind not in ASSIGNMENT_KINDS:
            problems.append(f"{where}: unknown assignment")
            continue
        if item.kind in ("starter", "subject_owner"):
            if item.value or item.expand:
                problems.append(f"{where}: {item.kind} takes no value")
        elif item.kind == "field":
            declared = fields.get(item.value)
            if declared is None or declared.type != "user":
                problems.append(f"{where}: assignment field {item.value!r} must be a declared user field")
        elif item.kind in ("role", "group"):
            if not isinstance(item.value, str) or not CODE.fullmatch(item.value) or len(item.value) > 64:
                problems.append(f"{where}: invalid {item.kind} code")
        elif not isinstance(item.value, str) or not item.value:
            problems.append(f"{where}: assignment rule needs a name")
        if item.expand and item.kind not in ("role", "group"):
            problems.append(f"{where}: only role and group assignments expand")
    return problems


def _human_problems(step: Step, out: list[Transition], spec: FlowSpec) -> list[str]:
    where = f"step {step.key}"
    problems: list[str] = []
    if not step.assignees:
        problems.append(f"{where}: a {step.kind} needs assignees")
    problems.extend(_assignment_problems(where, step.assignees, spec))
    if step.form is not None:
        problems.extend(_form_problems(f"{where} form", step.form))
    expire = False
    if step.sla is not None:
        sla = step.sla
        if type(sla.due_seconds) is not int or not 60 <= sla.due_seconds <= 366 * 86400:
            problems.append(f"{where}: the SLA is due between one minute and one year")
        if type(sla.business_hours) is not bool:
            problems.append(f"{where}: business_hours is true or false")
        if sla.on_overdue not in OVERDUE_ACTIONS:
            problems.append(f"{where}: unknown overdue action {sla.on_overdue!r}")
        if len(sla.remind_before_seconds) > 5 or any(
            type(item) is not int or not 0 < item < sla.due_seconds for item in sla.remind_before_seconds
        ):
            problems.append(f"{where}: reminders (at most five) come before the due time")
        if len(set(sla.remind_before_seconds)) != len(sla.remind_before_seconds):
            problems.append(f"{where}: duplicate reminder")
        problems.extend(_assignment_problems(f"{where} escalation", sla.escalate_to, spec))
        if sla.on_overdue == "reassign" and not sla.escalate_to:
            problems.append(f"{where}: reassigning an overdue task needs escalate_to")
        expire = sla.on_overdue == "expire"
    results = [item.on for item in out if item.on]
    for name in results:
        if name not in RESULTS[step.kind]:
            problems.append(f"{where}: unknown result {name!r} for a {step.kind}")
    if len(set(results)) != len(results):
        problems.append(f"{where}: one transition per result")
    if expire != ("expired" in results):
        problems.append(f"{where}: the expired transition exists exactly when the SLA expires the step")
    actions = [item for item in out if not item.on]
    if step.kind == "task":
        if not actions:
            problems.append(f"{where}: a task needs at least one action transition")
        if len(actions) > 1 and any(not item.key for item in actions):
            problems.append(f"{where}: with several actions each transition needs a key")
    else:
        if actions:
            problems.append(f"{where}: approval transitions are triggered by a result (on=...)")
        if "approved" not in results or "rejected" not in results:
            problems.append(f"{where}: an approval needs the approved and the rejected transitions")
        if any(item.condition is not None for item in out):
            problems.append(f"{where}: approval transitions take no condition")
        if step.policy not in POLICIES:
            problems.append(f"{where}: unknown approval policy {step.policy!r}")
        if step.policy == "quorum" and (type(step.quorum) is not int or step.quorum < 1):
            problems.append(f"{where}: a quorum approval needs quorum >= 1")
        if step.policy != "quorum" and step.quorum:
            problems.append(f"{where}: quorum applies only to the quorum policy")
    return problems


def _step_problems(step: Step, spec: FlowSpec) -> list[str]:
    where = f"step {step.key}"
    out, incoming = list(spec.outgoing(step.key)), list(spec.incoming(step.key))
    problems: list[str] = []
    keys = [item.key for item in out if item.key]
    if len(set(keys)) != len(keys):
        problems.append(f"{where}: duplicate transition key")
    if len({(item.target, item.on, item.key) for item in out}) != len(out):
        problems.append(f"{where}: duplicate transition")
    plain = all(not item.on and item.condition is None and not item.default and item.form is None for item in out)
    kind = step.kind
    if kind not in ("exclusive", "inclusive") and any(item.default for item in out):
        problems.append(f"{where}: only exclusive and inclusive gateways have a default transition")
    if kind not in ("exclusive", "inclusive", "task") and any(item.condition is not None for item in out):
        problems.append(f"{where}: conditions belong to gateways and task actions")
    if any(item.form is not None for item in out):
        if kind != "task" or any(item.form is not None and item.on for item in out):
            problems.append(f"{where}: only task actions carry a transition form")
        for item in out:
            if item.form is not None:
                problems.extend(_form_problems(f"{where} transition {item.ident}", item.form))
    if kind not in HUMAN_KINDS and (step.assignees or step.form is not None and kind != "wait" or step.sla is not None):
        problems.append(f"{where}: assignees, forms and SLA belong to tasks and approvals")
    if kind == "start":
        if incoming:
            problems.append(f"{where}: the start has no incoming transition")
        if len(out) != 1 or not plain:
            problems.append(f"{where}: the start has exactly one plain outgoing transition")
    elif kind == "end":
        if out:
            problems.append(f"{where}: an end has no outgoing transition")
        if not NAME.fullmatch(step.outcome or "") or len(step.outcome) > 64:
            problems.append(f"{where}: invalid outcome")
    elif kind in HUMAN_KINDS:
        problems.extend(_human_problems(step, out, spec))
    elif kind == "service":
        if not step.handler:
            problems.append(f"{where}: a service needs a handler")
        if step.outputs is not None:
            problems.extend(_form_problems(f"{where} outputs", step.outputs))
        if not 1 <= step.max_attempts <= 20 or not 1 <= step.retry_seconds <= 86400:
            problems.append(f"{where}: invalid retry settings")
        results = [item.on for item in out if item.on]
        if not out or len(set(results)) != len(results) or len(out) - len(results) > 1:
            problems.append(f"{where}: a service has one transition per result and at most one without a result")
        if any(not NAME.fullmatch(name) for name in results):
            problems.append(f"{where}: invalid result name")
    elif kind == "timer":
        if type(step.delay_seconds) is not int or not 0 <= step.delay_seconds <= 366 * 86400:
            problems.append(f"{where}: a timer waits between zero seconds and one year")
        if len(out) != 1 or not plain:
            problems.append(f"{where}: a timer has exactly one plain outgoing transition")
    elif kind == "wait":
        if step.event:
            if step.signal or not EVENT_CODE.fullmatch(step.event) or len(step.event) > 100:
                problems.append(f"{where}: a wait takes either a signal or a valid event type")
            if step.correlate and not NAME.fullmatch(step.correlate):
                problems.append(f"{where}: invalid correlation field")
            if step.form is not None:
                problems.append(f"{where}: an event wait has no form")
        elif not NAME.fullmatch(step.signal or "") or len(step.signal) > 64:
            problems.append(f"{where}: a wait needs a signal name")
        elif step.correlate:
            problems.append(f"{where}: only an event wait correlates")
        if step.form is not None:
            problems.extend(_form_problems(f"{where} form", step.form))
        results = [item.on or "received" for item in out]
        expected = ["received", "timeout"] if step.timeout_seconds else ["received"]
        if sorted(results) != expected:
            problems.append(f"{where}: a wait has the received transition and, with a timeout, the timeout one")
        if type(step.timeout_seconds) is not int or not 0 <= step.timeout_seconds <= 366 * 86400:
            problems.append(f"{where}: invalid timeout")
    elif kind == "subflow":
        if not FLOW_KEY.fullmatch(step.flow or "") or len(step.flow) > 100:
            problems.append(f"{where}: a subflow needs the key of the child flow")
        if type(step.flow_version) is not int or step.flow_version < 1:
            problems.append(f"{where}: a subflow pins a version from 1")
        results = [item.on for item in out]
        if not out or not all(results) or len(set(results)) != len(results):
            problems.append(f"{where}: a subflow has one transition per child outcome")
        if step.form is not None or step.assignees:
            problems.append(f"{where}: a subflow has no form nor assignees")
    elif kind == "exclusive" or kind == "inclusive":
        if len(out) < 2:
            problems.append(f"{where}: a gateway needs at least two outgoing transitions")
        if any(item.on or item.form is not None for item in out):
            problems.append(f"{where}: gateway transitions take only a condition or default")
        defaults = [item for item in out if item.default]
        if len(defaults) != 1 or defaults[0].condition is not None:
            problems.append(f"{where}: exactly one default transition, without a condition")
        if any(item.condition is None for item in out if not item.default):
            problems.append(f"{where}: every non-default transition needs a condition")
    elif kind == "parallel":
        if len(out) < 2 or not plain:
            problems.append(f"{where}: a parallel fork has at least two plain outgoing transitions")
    elif kind == "join":
        if len(incoming) < 2:
            problems.append(f"{where}: a join needs at least two incoming transitions")
        if len(out) != 1 or not plain:
            problems.append(f"{where}: a join has exactly one plain outgoing transition")
    return problems


def _block_problems(spec: FlowSpec, edges: Mapping[str, list[str]]) -> list[str]:
    problems: list[str] = []
    joins = {item.key for item in spec.steps if item.kind == "join"}
    owners: dict[str, list[str]] = {key: [] for key in joins}
    for split in (item for item in spec.steps if item.kind in SPLIT_KINDS):
        where = f"step {split.key}"
        if split.join not in joins:
            problems.append(f"{where}: fork without a join (join={split.join!r} is not a join step)")
            continue
        owners[split.join].append(split.key)
        stop = frozenset({split.join})
        branches = edges.get(split.key, [])
        for target in branches:
            if split.join not in _reach([target], edges, stop=stop):
                problems.append(f"{where}: branch {target} never reaches the join {split.join}")
        interior = _reach(branches, edges, stop=stop) - {split.join}
        if split.key in interior:
            problems.append(f"{where}: a branch returns to the fork without passing the join")
        for key in sorted(interior):
            step = spec.step(key)
            if step.kind == "end":
                problems.append(f"{where}: branch reaches the end {key} without passing the join")
            if step.kind in SPLIT_KINDS and step.join not in interior:
                problems.append(f"{where}: the nested fork {key} must join inside the block")
            if step.kind == "join" and not any(
                other.kind in SPLIT_KINDS and other.join == key and other.key in interior for other in spec.steps
            ):
                problems.append(f"{where}: the join {key} inside the block belongs to a fork outside it")
            for incoming in spec.incoming(key):
                if incoming.source not in interior and incoming.source != split.key:
                    problems.append(f"{where}: step {key} of the block is entered from outside ({incoming.source})")
        for incoming in spec.incoming(split.join):
            if incoming.source not in interior and incoming.source != split.key:
                problems.append(f"{where}: the join {split.join} is entered from outside the block ({incoming.source})")
    for key, splits in sorted(owners.items()):
        if len(splits) != 1:
            problems.append(f"step {key}: a join belongs to exactly one fork (found {len(splits)})")
    for step in spec.steps:
        if step.kind not in SPLIT_KINDS and step.join:
            problems.append(f"step {step.key}: only forks name a join")
    return problems


def structural_problems(spec: Any) -> list[str]:
    """Everything wrong with the shape of a flow; an empty list means the engine can run it."""
    if not isinstance(spec, FlowSpec):
        return ["not a FlowSpec"]
    problems: list[str] = []
    if not isinstance(spec.key, str) or not FLOW_KEY.fullmatch(spec.key) or len(spec.key) > 100:
        problems.append("key must look like app_label.name (up to 100 characters)")
    if type(spec.version) is not int or spec.version < 1:
        problems.append("version must be a positive integer")
    if not isinstance(spec.label, str) or not spec.label.strip() or len(spec.label) > 200:
        problems.append("label is required (up to 200 characters)")
    if spec.start_action not in ("read", "update"):
        problems.append("start_action must be read or update")
    if type(spec.retention_days) is not int or not 0 <= spec.retention_days <= MAX_RETENTION_DAYS:
        problems.append(f"retention_days must be an integer from 0 to {MAX_RETENTION_DAYS}")
    if not spec.steps or len(spec.steps) > MAX_STEPS or len(spec.transitions) > 4 * MAX_STEPS:
        return [*problems, f"a flow declares between 1 and {MAX_STEPS} steps"]
    keys = [item.key for item in spec.steps]
    for item in spec.steps:
        if not isinstance(item, Step) or not isinstance(item.key, str) or not NAME.fullmatch(item.key):
            problems.append(f"step {getattr(item, 'key', item)!r}: invalid step key")
        elif len(item.key) > 64 or len(item.label) > 200:
            problems.append(f"step {item.key}: key or label too long")
        elif item.kind not in STEP_KINDS:
            problems.append(f"step {item.key}: unknown step kind {item.kind!r}")
    if len(set(keys)) != len(keys):
        problems.append("duplicate step key")
    for item in spec.transitions:
        if not isinstance(item, Transition) or item.source not in keys or item.target not in keys:
            problems.append(f"transition {getattr(item, 'ident', item)!r}: unknown source or target step")
        elif item.key and (not NAME.fullmatch(item.key) or len(item.key) > 64):
            problems.append(f"transition {item.ident}: invalid key")
        elif len(item.label) > 200 or len(item.on) > 64:
            problems.append(f"transition {item.ident}: label or result too long")
    if problems:
        return problems  # The graph below needs sound keys.
    starts = [item.key for item in spec.steps if item.kind == "start"]
    ends = [item.key for item in spec.steps if item.kind == "end"]
    if len(starts) != 1:
        problems.append(f"a flow has exactly one start (found {len(starts)})")
    if not ends:
        problems.append("a flow needs at least one end")
    if spec.start_form is not None:
        problems.extend(_form_problems("start form", spec.start_form))
    for item in spec.steps:
        problems.extend(_step_problems(item, spec))
    problems.extend(_field_coherence(spec))
    edges: dict[str, list[str]] = {key: [] for key in keys}
    reverse: dict[str, list[str]] = {key: [] for key in keys}
    for item in spec.transitions:
        edges[item.source].append(item.target)
        reverse[item.target].append(item.source)
    if len(starts) == 1:
        reachable = _reach(starts, edges)
        problems.extend(f"step {key}: unreachable from the start" for key in keys if key not in reachable)
    if ends:
        finishing = _reach(ends, reverse)
        problems.extend(f"step {key}: dead end (no path to an end)" for key in keys if key not in finishing)
    problems.extend(_block_problems(spec, edges))
    fields = spec.data_fields()
    step_keys = frozenset(keys)
    for item in spec.transitions:
        if item.condition is None:
            continue
        # Registered names are checked by reference_problems; here every name is accepted.
        found = expressions.problems(item.condition, fields=fields, steps=step_keys, conditions=_AnyName(), _depth=0)
        problems.extend(f"transition {item.ident}: {problem}" for problem in found)
    if len(fields) > MAX_FIELDS:
        problems.append(f"a flow declares at most {MAX_FIELDS} distinct fields")
    for moment, code in spec.subject_events:
        if moment not in MOMENTS or not isinstance(code, str) or not code:
            problems.append(f"subject event {moment!r}: unknown moment or empty event type")
    if len({moment for moment, _ in spec.subject_events}) != len(spec.subject_events):
        problems.append("subject events: one event type per moment")
    return problems


class _AnyName(frozenset):  # type: ignore[type-arg]
    """Accepts every registered name; the structural pass does not know the registries."""

    def __contains__(self, item: object) -> bool:
        return isinstance(item, str) and bool(item)


def _named_conditions(expr: Any) -> list[str]:
    if expr is None:
        return []
    if expr.op == "call":
        return [expr.name]
    return [name for item in expr.items for name in _named_conditions(item)]


def reference_problems(
    spec: FlowSpec,
    *,
    handlers: Mapping[str, str],
    conditions: Mapping[str, str],
    assignments: Mapping[str, str],
) -> list[str]:
    """Names the flow refers to: each must be registered in code by the flow's own app.

    The mappings are ``name -> owner_app``. Tables, sequences and event types are looked up
    in their registries; none of this touches the database.
    """
    problems: list[str] = []

    def owned(kind: str, name: str, registry: Mapping[str, str]) -> None:
        if name not in registry:
            problems.append(f"{kind} {name!r} is not registered")
        elif registry[name] != spec.owner_app:
            problems.append(f"{kind} {name!r} belongs to another app")

    for step in spec.steps:
        if step.kind == "service" and step.handler:
            owned("handler", step.handler, handlers)
        rules = [*step.assignees, *(step.sla.escalate_to if step.sla else ())]
        for item in rules:
            if isinstance(item, Assignment) and item.kind == "rule" and item.value:
                owned("assignment rule", item.value, assignments)
    for transition in spec.transitions:
        for name in _named_conditions(transition.condition):
            owned("condition", name, conditions)
    problems.extend(_subject_problems(spec))
    return problems


def _subject_problems(spec: FlowSpec) -> list[str]:
    from skeleton.common.models import SecuredRecord
    from skeleton.system.registry import get_spec

    problems: list[str] = []
    try:
        table = get_spec(spec.subject_table)
    except KeyError:
        return [f"subject table {spec.subject_table!r} has no TableSpec"]
    model = table.model
    if not issubclass(model, SecuredRecord) or not table.expose_api:
        problems.append(f"subject table {spec.subject_table!r} must be an exposed SecuredRecord")
    if model._meta.app_label != spec.owner_app:
        problems.append(f"subject table {spec.subject_table!r} belongs to another app")
    if spec.number_sequence:
        from skeleton.sequences.registry import get_sequence

        try:
            sequence = get_sequence(spec.number_sequence)
        except KeyError:
            problems.append(f"number sequence {spec.number_sequence!r} is not declared")
        else:
            if sequence.owner_app != spec.owner_app:
                problems.append(f"number sequence {spec.number_sequence!r} belongs to another app")
    from skeleton.events.registry import get_event_type

    from .registry import get_flow

    for step in spec.steps:
        if step.kind != "subflow":
            continue
        try:
            child = get_flow(step.flow)
        except KeyError:
            problems.append(f"step {step.key}: child flow {step.flow!r} is not declared in code")
            continue
        if child.owner_app != spec.owner_app:
            problems.append(f"step {step.key}: child flow {step.flow!r} must be the app's own")
        if child.subject_table != spec.subject_table:
            problems.append(f"step {step.key}: child flow {step.flow!r} must run on the same table")
        if step.flow == spec.key:
            problems.append(f"step {step.key}: a flow cannot start itself")
    for step in spec.steps:
        if step.kind != "wait" or not step.event:
            continue
        try:
            awaited = get_event_type(step.event)
        except KeyError:
            problems.append(f"step {step.key}: event type {step.event!r} is not registered")
            continue
        if awaited.owner_app != spec.owner_app:
            problems.append(f"step {step.key}: event type {step.event!r} must be the app's own")
        elif step.correlate and step.correlate not in awaited.payload_fields:
            problems.append(f"step {step.key}: {step.correlate!r} is not a payload field of {step.event!r}")
        elif not step.correlate and awaited.table_key != spec.subject_table:
            problems.append(f"step {step.key}: event type {step.event!r} is not about the subject; correlate a field")
    for moment, code in spec.subject_events:
        try:
            event_type = get_event_type(code)
        except KeyError:
            problems.append(f"subject event {moment}: event type {code!r} is not registered")
            continue
        if event_type.table_key != spec.subject_table or event_type.owner_app != spec.owner_app:
            problems.append(f"subject event {moment}: event type {code!r} must be the app's own, about the subject")
        if event_type.payload_fields:
            problems.append(f"subject event {moment}: event type {code!r} must declare no payload fields")
    return problems
