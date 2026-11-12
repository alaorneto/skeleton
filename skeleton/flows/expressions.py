"""Safe conditions: a tree of data over a closed operator list (docs/flows.md, "Condições").

``problems()`` validates a tree against the declared fields before a flow is accepted, and
``evaluate()`` walks it. Nothing is parsed from text and nothing is evaluated with ``eval``:
an unknown operator is a validation problem, and ``call`` resolves a name in the registry
of conditions that apps declare in code.
"""

from __future__ import annotations

import operator
from collections.abc import Callable, Mapping
from typing import Any

from .spec import OPERATORS, OUTCOME_PREFIX, Expr, Field
from .values import ORDERED_TYPES, InvalidValue, decode, parse

MAX_DEPTH = 8
MAX_NODES = 64
_COMPARE: dict[str, Callable[[Any, Any], bool]] = {
    "eq": operator.eq,
    "ne": operator.ne,
    "gt": operator.gt,
    "ge": operator.ge,
    "lt": operator.lt,
    "le": operator.le,
}
_OUTCOME = Field(name="@outcome", type="str")


def _field(name: str, fields: Mapping[str, Field], steps: frozenset[str]) -> Field | None:
    if name.startswith(OUTCOME_PREFIX):
        return _OUTCOME if name[len(OUTCOME_PREFIX) :] in steps else None
    return fields.get(name)


def problems(
    expr: Any, *, fields: Mapping[str, Field], steps: frozenset[str], conditions: frozenset[str], _depth: int = 0
) -> list[str]:
    """Why a condition tree is not acceptable; an empty list means it is safe to evaluate."""
    if not isinstance(expr, Expr):
        return ["condition is not an expression"]
    if _depth > MAX_DEPTH:
        return ["condition is nested too deeply"]
    if expr.op not in OPERATORS:
        return [f"unknown condition operator {expr.op!r}"]
    if expr.op in ("all", "any", "not"):
        if not expr.items or (expr.op == "not" and len(expr.items) != 1) or len(expr.items) > MAX_NODES:
            return [f"condition {expr.op} needs its operands"]
        found: list[str] = []
        for item in expr.items:
            found.extend(problems(item, fields=fields, steps=steps, conditions=conditions, _depth=_depth + 1))
        return found
    if expr.op == "call":
        if expr.name not in conditions:
            return [f"condition {expr.name!r} is not registered by the flow's app"]
        return []
    declared = _field(expr.field, fields, steps)
    if declared is None:
        return [f"condition refers to the undeclared field {expr.field!r}"]
    if declared.sensitive and expr.op != "truthy":
        # A literal next to a sensitive field would copy the secret into the frozen definition.
        return [f"condition compares the sensitive field {expr.field!r}; use a registered condition"]
    if expr.op == "truthy":
        return []
    if expr.op in ("gt", "ge", "lt", "le") and declared.type not in ORDERED_TYPES:
        return [f"condition orders the field {expr.field!r}, which has no order"]
    literals = expr.value if expr.op == "in" else (expr.value,)
    if expr.op == "in" and (not isinstance(literals, tuple) or not literals or len(literals) > MAX_NODES):
        return [f"condition on {expr.field!r} needs a list of values"]
    for literal in literals:
        if literal is None and expr.op in ("eq", "ne"):
            continue
        try:
            if parse(declared, literal) is None:
                raise InvalidValue("empty")
        except InvalidValue:
            return [f"condition compares {expr.field!r} with a value that is not a {declared.type}"]
    return []


class Evaluation:
    """Read-only inputs of an evaluation: stored data, step outcomes and callable conditions."""

    def __init__(
        self,
        *,
        fields: Mapping[str, Field],
        data: Mapping[str, Any],
        outcomes: Mapping[str, str],
        call: Callable[[str], bool] | None = None,
    ) -> None:
        self.fields = fields
        self.data = data
        self.outcomes = outcomes
        self.call = call

    def value(self, name: str) -> tuple[Field, Any]:
        if name.startswith(OUTCOME_PREFIX):
            return _OUTCOME, self.outcomes.get(name[len(OUTCOME_PREFIX) :]) or None
        declared = self.fields[name]
        return declared, decode(declared, self.data.get(name))


class _Unsafe(Exception):
    """An expression that validation would have refused; the whole evaluation is false."""


def evaluate(expr: Expr, context: Evaluation) -> bool:
    """The truth of a validated condition; anything unexpected makes the whole tree ``False``.

    Failing closed is decided at the root: an unsafe node under a ``not`` must not become true.
    """
    try:
        return _walk(expr, context, 0)
    except _Unsafe:
        return False


def _walk(expr: Expr, context: Evaluation, depth: int) -> bool:
    if depth > MAX_DEPTH or not isinstance(expr, Expr) or expr.op not in OPERATORS:
        raise _Unsafe
    if expr.op == "all":
        return all(_walk(item, context, depth + 1) for item in expr.items)
    if expr.op == "any":
        return any(_walk(item, context, depth + 1) for item in expr.items)
    if expr.op == "not":
        if len(expr.items) != 1:
            raise _Unsafe
        return not _walk(expr.items[0], context, depth + 1)
    if expr.op == "call":
        if context.call is None:
            raise _Unsafe
        return bool(context.call(expr.name))
    try:
        declared, current = context.value(expr.field)
    except KeyError:
        raise _Unsafe from None
    if expr.op == "truthy":
        return bool(current)
    if expr.op == "in":
        if not isinstance(expr.value, tuple):
            raise _Unsafe
        return current is not None and any(current == decode(declared, literal) for literal in expr.value)
    literal = None if expr.value is None else decode(declared, expr.value)
    if expr.op in ("eq", "ne"):
        return bool(_COMPARE[expr.op](current, literal))
    if current is None or literal is None:
        return False
    try:
        return bool(_COMPARE[expr.op](current, literal))
    except TypeError:
        raise _Unsafe from None
