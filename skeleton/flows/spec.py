"""Declarative specification of a flow: frozen dataclasses with a canonical JSON form (docs/flows.md).

A specification is pure data. Conditions are expression trees over a closed operator list,
and handlers, callable conditions and assignment rules are referred to by a name that the
engine resolves in registries declared in code: nothing here is ever evaluated as text, and
no class or module name comes from stored data.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

FIELD_TYPES = ("str", "text", "int", "decimal", "bool", "date", "datetime", "choice", "user")
STEP_KINDS = (
    "start",
    "end",
    "task",
    "approval",
    "service",
    "timer",
    "wait",
    "subflow",
    "exclusive",
    "parallel",
    "inclusive",
    "join",
)
HUMAN_KINDS = ("task", "approval")
SPLIT_KINDS = ("parallel", "inclusive")
POLICIES = ("any", "all", "quorum", "sequential")
ASSIGNMENT_KINDS = ("starter", "subject_owner", "field", "role", "group", "rule")
OVERDUE_ACTIONS = ("notify", "reassign", "expire")
MOMENTS = ("started", "completed", "cancelled", "failed")
COMPARISONS = ("eq", "ne", "gt", "ge", "lt", "le")
OPERATORS = (*COMPARISONS, "in", "truthy", "all", "any", "not", "call")
OUTCOME_PREFIX = "@outcome."

Scalar = str | int | bool | None


def _seconds(value: Any) -> int:
    if isinstance(value, timedelta):
        return int(value.total_seconds())
    if type(value) is int:
        return value
    raise TypeError("Durations are whole seconds or a timedelta.")


def _literal(value: Any) -> Any:
    """A JSON scalar for a condition literal; decimals and dates travel as text."""
    if value is None or type(value) in (str, int, bool):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    raise TypeError("Condition literals are text, integers, booleans, decimals or dates.")


@dataclass(frozen=True)
class Field:
    """One declared form field; anything not declared is refused, never ignored."""

    name: str
    type: str = "str"
    label: str = ""
    required: bool = False
    sensitive: bool = False
    max_length: int = 0
    choices: tuple[str, ...] = ()
    minimum: str = ""
    maximum: str = ""
    help_text: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "choices", tuple(self.choices))
        for name in ("minimum", "maximum"):
            value = getattr(self, name)
            if value is None:
                value = ""
            if isinstance(value, int | Decimal) and not isinstance(value, bool):
                value = str(value)
            object.__setattr__(self, name, value)


@dataclass(frozen=True, init=False)
class Form:
    fields: tuple[Field, ...]

    def __init__(self, *fields: Field) -> None:
        object.__setattr__(self, "fields", tuple(fields))

    def names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.fields)


@dataclass(frozen=True)
class Assignment:
    """Who gets a task, in terms every organization can resolve (codes, never UUIDs)."""

    kind: str
    value: str = ""
    expand: bool = False

    @classmethod
    def starter(cls) -> Assignment:
        return cls("starter")

    @classmethod
    def subject_owner(cls) -> Assignment:
        return cls("subject_owner")

    @classmethod
    def field(cls, name: str) -> Assignment:
        return cls("field", name)

    @classmethod
    def role(cls, code: str, *, expand: bool = False) -> Assignment:
        return cls("role", code, expand)

    @classmethod
    def group(cls, code: str, *, expand: bool = False) -> Assignment:
        return cls("group", code, expand)

    @classmethod
    def rule(cls, name: str) -> Assignment:
        return cls("rule", name)


@dataclass(frozen=True)
class Sla:
    due_seconds: int
    remind_before_seconds: tuple[int, ...] = ()
    on_overdue: str = "notify"
    escalate_to: tuple[Assignment, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "due_seconds", _seconds(self.due_seconds))
        object.__setattr__(self, "remind_before_seconds", tuple(_seconds(item) for item in self.remind_before_seconds))
        object.__setattr__(self, "escalate_to", tuple(self.escalate_to))


@dataclass(frozen=True)
class Expr:
    """A node of a condition tree: a comparison, a combinator or a registered callable by name."""

    op: str
    field: str = ""
    value: Any = None
    items: tuple[Expr, ...] = ()
    name: str = ""


def _comparison(op: str, name: str, value: Any) -> Expr:
    return Expr(op, field=name, value=_literal(value))


def eq(name: str, value: Any) -> Expr:
    return _comparison("eq", name, value)


def ne(name: str, value: Any) -> Expr:
    return _comparison("ne", name, value)


def gt(name: str, value: Any) -> Expr:
    return _comparison("gt", name, value)


def ge(name: str, value: Any) -> Expr:
    return _comparison("ge", name, value)


def lt(name: str, value: Any) -> Expr:
    return _comparison("lt", name, value)


def le(name: str, value: Any) -> Expr:
    return _comparison("le", name, value)


def is_in(name: str, values: Any) -> Expr:
    return Expr("in", field=name, value=tuple(_literal(item) for item in values))


def truthy(name: str) -> Expr:
    return Expr("truthy", field=name)


def all_of(*items: Expr) -> Expr:
    return Expr("all", items=tuple(items))


def any_of(*items: Expr) -> Expr:
    return Expr("any", items=tuple(items))


def negate(item: Expr) -> Expr:
    return Expr("not", items=(item,))


def call(name: str) -> Expr:
    """A condition registered by the app with ``register_flow_condition``."""
    return Expr("call", name=name)


def outcome(step_key: str) -> str:
    """The field name of the latest outcome of a step, for comparisons (``eq(outcome("x"), "approved")``)."""
    return OUTCOME_PREFIX + step_key


@dataclass(frozen=True)
class Step:
    key: str
    kind: str
    label: str = ""
    assignees: tuple[Assignment, ...] = ()
    form: Form | None = None
    sla: Sla | None = None
    delegable: bool = True
    policy: str = "any"
    quorum: int = 0
    handler: str = ""
    outputs: Form | None = None
    max_attempts: int = 3
    retry_seconds: int = 60
    delay_seconds: int = 0
    signal: str = ""
    # A wait advanced by a domain event instead of a signal (#411); see ``wait``.
    event: str = ""
    correlate: str = ""
    # A subflow (#412): the child flow and the version pinned for it.
    flow: str = ""
    flow_version: int = 0
    timeout_seconds: int = 0
    outcome: str = ""
    join: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "assignees", tuple(self.assignees))

    @property
    def title(self) -> str:
        return self.label or self.key


def start(key: str, *, label: str = "") -> Step:
    return Step(key, "start", label)


def end(key: str, *, outcome: str = "", label: str = "") -> Step:
    return Step(key, "end", label, outcome=outcome or key)


def task(
    key: str,
    *,
    assignees: tuple[Assignment, ...],
    label: str = "",
    form: Form | None = None,
    sla: Sla | None = None,
    delegable: bool = True,
) -> Step:
    return Step(key, "task", label, assignees=assignees, form=form, sla=sla, delegable=delegable)


def approval(
    key: str,
    *,
    assignees: tuple[Assignment, ...],
    label: str = "",
    policy: str = "any",
    quorum: int = 0,
    form: Form | None = None,
    sla: Sla | None = None,
    delegable: bool = True,
) -> Step:
    return Step(
        key,
        "approval",
        label,
        assignees=assignees,
        form=form,
        sla=sla,
        delegable=delegable,
        policy=policy,
        quorum=quorum,
    )


def service(
    key: str,
    *,
    handler: str,
    label: str = "",
    outputs: Form | None = None,
    max_attempts: int = 3,
    retry_seconds: Any = 60,
) -> Step:
    return Step(
        key,
        "service",
        label,
        handler=handler,
        outputs=outputs,
        max_attempts=max_attempts,
        retry_seconds=_seconds(retry_seconds),
    )


def timer(key: str, *, delay_seconds: Any, label: str = "") -> Step:
    return Step(key, "timer", label, delay_seconds=_seconds(delay_seconds))


def wait(
    key: str,
    *,
    signal: str = "",
    event: str = "",
    correlate: str = "",
    label: str = "",
    timeout_seconds: Any = 0,
    form: Form | None = None,
) -> Step:
    """Wait for a ``signal`` sent by the app, or for a domain ``event`` of the app (#411).

    An event wait advances when an event of that type is published about the instance's
    subject record, or, with ``correlate=<payload field>``, when that field of the event holds
    the subject's sysid. Exactly one of ``signal`` and ``event``; an event wait has no form.
    """
    return Step(
        key,
        "wait",
        label,
        signal=signal,
        event=event,
        correlate=correlate,
        timeout_seconds=_seconds(timeout_seconds),
        form=form,
    )


def subflow(key: str, *, flow: str, version: int, label: str = "") -> Step:
    """Start the flow ``flow`` at the pinned ``version`` on the same record, and wait for its end (#412).

    The transitions take the child's outcome (``on="approved"``); a failed child fails this step,
    and cancelling or suspending this flow does the same to the child.
    """
    return Step(key, "subflow", label, flow=flow, flow_version=version)


def exclusive(key: str, *, label: str = "") -> Step:
    return Step(key, "exclusive", label)


def parallel(key: str, *, join: str, label: str = "") -> Step:
    return Step(key, "parallel", label, join=join)


def inclusive(key: str, *, join: str, label: str = "") -> Step:
    return Step(key, "inclusive", label, join=join)


def join(key: str, *, label: str = "") -> Step:
    return Step(key, "join", label)


@dataclass(frozen=True)
class Transition:
    source: str
    target: str
    key: str = ""
    label: str = ""
    on: str = ""
    condition: Expr | None = None
    default: bool = False
    form: Form | None = None

    @property
    def ident(self) -> str:
        """Stable identity inside the flow: the declared key, or ``source>target`` (plus ``:on``)."""
        return self.key or f"{self.source}>{self.target}" + (f":{self.on}" if self.on else "")


# Step fields added after the first release, with the default that keeps them out of the
# canonical form (and so out of the digest of every flow frozen before them).
OPTIONAL_STEP_FIELDS = {"event": "", "correlate": "", "flow": "", "flow_version": 0}


@dataclass(frozen=True)
class FlowSpec:
    key: str
    version: int
    label: str
    owner_app: str
    subject_table: str
    steps: tuple[Step, ...]
    transitions: tuple[Transition, ...]
    start_form: Form | None = None
    start_action: str = "update"
    starter_can_cancel: bool = True
    number_sequence: str = ""
    # ((moment, event type code), ...): app events about the subject record, which reach webhooks.
    subject_events: tuple[tuple[str, str], ...] = ()
    _index: dict[str, Any] = field(default_factory=dict, compare=False, repr=False, hash=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "steps", tuple(self.steps))
        object.__setattr__(self, "transitions", tuple(self.transitions))
        object.__setattr__(self, "subject_events", tuple((moment, code) for moment, code in self.subject_events))

    def __hash__(self) -> int:
        return hash((self.key, self.version))

    # Lookups (a specification is immutable, so they are computed once).
    def _cached(self, name: str, build: Any) -> Any:
        if name not in self._index:
            self._index[name] = build()
        return self._index[name]

    def step(self, key: str) -> Step:
        return self._cached("steps", lambda: {item.key: item for item in self.steps})[key]

    def has_step(self, key: str) -> bool:
        return key in self._cached("steps", lambda: {item.key: item for item in self.steps})

    def outgoing(self, key: str) -> tuple[Transition, ...]:
        return tuple(item for item in self.transitions if item.source == key)

    def incoming(self, key: str) -> tuple[Transition, ...]:
        return tuple(item for item in self.transitions if item.target == key)

    def start_step(self) -> Step:
        return next(item for item in self.steps if item.kind == "start")

    def forms(self) -> tuple[Form, ...]:
        found = [self.start_form] if self.start_form else []
        for item in self.steps:
            found.extend(form for form in (item.form, item.outputs) if form is not None)
        found.extend(item.form for item in self.transitions if item.form is not None)
        return tuple(found)

    def data_fields(self) -> dict[str, Field]:
        """Every declared field by name (the first declaration; the check refuses divergent ones)."""

        def build() -> dict[str, Field]:
            fields: dict[str, Field] = {}
            for form in self.forms():
                for item in form.fields:
                    fields.setdefault(item.name, item)
            return fields

        return self._cached("fields", build)

    def subject_event(self, moment: str) -> str:
        return dict(self.subject_events).get(moment, "")

    # Canonical form.
    def to_json(self) -> dict[str, Any]:
        raw = dataclasses.asdict(self)
        raw.pop("_index", None)
        for step in raw["steps"]:
            # Fields added after the first release stay out of the canonical form while empty,
            # so the digest of every flow frozen before them does not change (#411).
            for name, default in OPTIONAL_STEP_FIELDS.items():
                if step.get(name) == default:
                    step.pop(name)
        return json.loads(json.dumps(raw))

    def canonical(self) -> str:
        return json.dumps(self.to_json(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    def digest(self) -> str:
        return self._cached("digest", lambda: hashlib.sha256(self.canonical().encode("ascii")).hexdigest())

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> FlowSpec:
        return cls(
            key=data["key"],
            version=data["version"],
            label=data["label"],
            owner_app=data["owner_app"],
            subject_table=data["subject_table"],
            steps=tuple(_step(item) for item in data["steps"]),
            transitions=tuple(_transition(item) for item in data["transitions"]),
            start_form=_form(data.get("start_form")),
            start_action=data.get("start_action", "update"),
            starter_can_cancel=data.get("starter_can_cancel", True),
            number_sequence=data.get("number_sequence", ""),
            subject_events=tuple((moment, code) for moment, code in data.get("subject_events", ())),
        )


def _form(data: Any) -> Form | None:
    if data is None:
        return None
    return Form(*(Field(**{**item, "choices": tuple(item.get("choices", ()))}) for item in data["fields"]))


def _assignment(data: dict[str, Any]) -> Assignment:
    return Assignment(data["kind"], data.get("value", ""), data.get("expand", False))


def _sla(data: Any) -> Sla | None:
    if data is None:
        return None
    return Sla(
        due_seconds=data["due_seconds"],
        remind_before_seconds=tuple(data.get("remind_before_seconds", ())),
        on_overdue=data.get("on_overdue", "notify"),
        escalate_to=tuple(_assignment(item) for item in data.get("escalate_to", ())),
    )


def _expr(data: Any) -> Expr | None:
    if data is None:
        return None
    value = data.get("value")
    return Expr(
        op=data["op"],
        field=data.get("field", ""),
        value=tuple(value) if isinstance(value, list) else value,
        items=tuple(item for item in (_expr(child) for child in data.get("items", ())) if item is not None),
        name=data.get("name", ""),
    )


def _step(data: dict[str, Any]) -> Step:
    values = dict(data)
    values["assignees"] = tuple(_assignment(item) for item in data.get("assignees", ()))
    values["form"] = _form(data.get("form"))
    values["outputs"] = _form(data.get("outputs"))
    values["sla"] = _sla(data.get("sla"))
    return Step(**values)


def _transition(data: dict[str, Any]) -> Transition:
    values = dict(data)
    values["condition"] = _expr(data.get("condition"))
    values["form"] = _form(data.get("form"))
    return Transition(**values)
