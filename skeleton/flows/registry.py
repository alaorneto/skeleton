"""Flows, service handlers, conditions and assignment rules are declared in code (docs/flows.md).

This is the public module apps import: the specification builders are re-exported here, and
``register_*`` is called in ``AppConfig.ready()``. A stored definition only ever refers to
these registries by name; no import path or class name comes from data.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any
from uuid import UUID

from .spec import (
    Assignment,
    Expr,
    Field,
    FlowSpec,
    Form,
    Sla,
    Step,
    Transition,
    all_of,
    any_of,
    approval,
    call,
    end,
    eq,
    exclusive,
    ge,
    gt,
    inclusive,
    is_in,
    join,
    le,
    lt,
    ne,
    negate,
    outcome,
    parallel,
    service,
    start,
    subflow,
    task,
    timer,
    truthy,
    wait,
)

__all__ = [
    "Assignees",
    "Assignment",
    "Expr",
    "Field",
    "FlowContext",
    "FlowSpec",
    "Form",
    "Sla",
    "Step",
    "StepResult",
    "Transition",
    "all_flows",
    "all_of",
    "any_of",
    "approval",
    "call",
    "end",
    "eq",
    "exclusive",
    "flow_problems",
    "ge",
    "get_flow",
    "gt",
    "inclusive",
    "is_in",
    "join",
    "le",
    "lt",
    "ne",
    "negate",
    "outcome",
    "parallel",
    "register_flow",
    "register_flow_assignment",
    "register_flow_condition",
    "register_flow_handler",
    "service",
    "start",
    "subflow",
    "task",
    "timer",
    "truthy",
    "wait",
]


@dataclass(frozen=True, slots=True)
class FlowContext:
    """What a handler, a condition or an assignment rule receives: identifiers and read-only data.

    It grants nothing: the callable runs in the caller's (or the job's system) context and
    re-authorizes whatever it reads. ``data`` holds the stored JSON scalars of the non-sensitive
    fields; ``sensitive`` those of the sensitive ones.
    """

    organization_id: UUID
    instance_id: UUID
    flow_key: str
    flow_version: int
    step_key: str
    subject_table: str
    subject_id: UUID
    starter_id: UUID | None
    data: Mapping[str, Any]
    sensitive: Mapping[str, Any]
    outcomes: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class StepResult:
    """What a service handler may return: the result that picks the transition and declared outputs."""

    outcome: str = ""
    data: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class Assignees:
    """What an assignment rule returns: people, groups and roles of the current organization."""

    users: tuple[UUID, ...] = ()
    groups: tuple[UUID, ...] = ()
    roles: tuple[UUID, ...] = ()


@dataclass(frozen=True)
class Registered:
    name: str
    function: Callable[..., Any]
    owner_app: str


_flows: dict[str, FlowSpec] = {}
_handlers: dict[str, Registered] = {}
_conditions: dict[str, Registered] = {}
_assignments: dict[str, Registered] = {}
_duplicates: list[str] = []


def register_flow(spec: FlowSpec) -> FlowSpec:
    """Declare the published version of a flow; a different spec under the same key is reported by E029."""
    existing = _flows.get(spec.key)
    if existing is not None and existing != spec:
        _duplicates.append(f"flow {spec.key}")
        return existing
    _flows[spec.key] = spec
    return spec


def get_flow(key: str) -> FlowSpec:
    return _flows[key]


def all_flows() -> tuple[FlowSpec, ...]:
    return tuple(_flows[key] for key in sorted(_flows))


def _register(registry: dict[str, Registered], kind: str, name: str, function: Callable[..., Any], owner_app: str):
    entry = Registered(name, function, owner_app)
    existing = registry.get(name)
    if existing is not None and existing != entry:
        _duplicates.append(f"{kind} {name}")
        return existing
    registry[name] = entry
    return entry


def register_flow_handler(name: str, function: Callable[[FlowContext], Any], *, owner_app: str) -> Registered:
    """A service step handler: ``function(context) -> None | str | StepResult``, run by a job as system."""
    return _register(_handlers, "handler", name, function, owner_app)


def register_flow_condition(name: str, function: Callable[[FlowContext], bool], *, owner_app: str) -> Registered:
    """A condition too rich for an expression: ``function(context) -> bool``."""
    return _register(_conditions, "condition", name, function, owner_app)


def register_flow_assignment(name: str, function: Callable[[FlowContext], Assignees], *, owner_app: str) -> Registered:
    """An assignment rule: ``function(context) -> Assignees`` of the current organization."""
    return _register(_assignments, "assignment rule", name, function, owner_app)


def get_handler(name: str) -> Registered:
    return _handlers[name]


def get_condition(name: str) -> Registered:
    return _conditions[name]


def get_assignment(name: str) -> Registered:
    return _assignments[name]


def registered_names() -> dict[str, tuple[tuple[str, str], ...]]:
    """(name, owner app) of every registered handler, condition and assignment rule, sorted.

    What a flow authored as data may reference (#414, #415).
    """
    return {
        kind: tuple((name, registry[name].owner_app) for name in sorted(registry))
        for kind, registry in (("handlers", _handlers), ("conditions", _conditions), ("assignments", _assignments))
    }


def spec_problems(spec: FlowSpec) -> list[str]:
    """Structural and reference problems of one flow against the current registries."""
    from .validation import reference_problems, structural_problems

    problems = structural_problems(spec)
    if problems:
        return problems
    owners = lambda registry: {name: entry.owner_app for name, entry in registry.items()}  # noqa: E731
    return reference_problems(
        spec, handlers=owners(_handlers), conditions=owners(_conditions), assignments=owners(_assignments)
    )


def _subflow_cycles() -> list[str]:
    """Flows declared in code that start each other in a loop (#412)."""
    edges = {
        spec.key: sorted({step.flow for step in spec.steps if step.kind == "subflow" and step.flow in _flows})
        for spec in all_flows()
    }
    problems: list[str] = []
    state: dict[str, int] = {}

    def visit(key: str, path: list[str]) -> None:
        state[key] = 1
        for target in edges.get(key, ()):
            if state.get(target) == 1:
                cycle = path[path.index(target) :] + [target]
                problems.append("subflow cycle: " + " -> ".join(cycle))
            elif target not in state:
                visit(target, [*path, target])
        state[key] = 2

    for key in sorted(edges):
        if key not in state:
            visit(key, [key])
    return problems


def flow_problems() -> list[str]:
    """Every problem the system check ``skeleton.E029`` reports."""
    problems = [f"{name}: duplicate registration" for name in sorted(set(_duplicates))]
    for registry, kind in ((_handlers, "handler"), (_conditions, "condition"), (_assignments, "assignment rule")):
        for name in sorted(registry):
            entry = registry[name]
            if not callable(entry.function):
                problems.append(f"{kind} {name}: not callable")
            if not entry.owner_app:
                problems.append(f"{kind} {name}: owner_app is required")
    problems.extend(_subflow_cycles())
    for spec in all_flows():
        if not spec.owner_app:
            problems.append(f"{spec.key}: owner_app is required")
        problems.extend(f"{spec.key}: {problem}" for problem in spec_problems(spec))
    return problems
