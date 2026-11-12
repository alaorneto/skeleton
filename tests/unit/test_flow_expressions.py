"""Conditions are data over a closed operator list: validated first, never evaluated as text (#201)."""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from skeleton.flows import registry as flows
from skeleton.flows.expressions import Evaluation, evaluate, problems
from skeleton.flows.spec import Expr, Field
from skeleton.flows.values import InvalidValue, check_bounds, decode, encode, parse

FIELDS = {
    "amount": Field("amount", "decimal"),
    "count": Field("count", "int"),
    "audience": Field("audience", "choice", choices=("internal", "public")),
    "title": Field("title", "str"),
    "urgent": Field("urgent", "bool"),
    "due": Field("due", "date"),
    "at": Field("at", "datetime"),
    "reviewer": Field("reviewer", "user"),
    "secret": Field("secret", "str", sensitive=True),
}
STEPS = frozenset({"legal", "editor"})


def check(expr, conditions=frozenset()):
    return problems(expr, fields=FIELDS, steps=STEPS, conditions=conditions)


def run(expr, data=None, outcomes=None, call=None):
    return evaluate(expr, Evaluation(fields=FIELDS, data=data or {}, outcomes=outcomes or {}, call=call))


def test_typed_comparisons_never_compare_as_text():
    assert check(flows.gt("amount", 1000)) == []
    assert run(flows.gt("amount", 1000), {"amount": "900"}) is False  # "900" > "1000" as text.
    assert run(flows.gt("amount", 1000), {"amount": "1000.01"}) is True
    assert run(flows.le("amount", Decimal("10.5")), {"amount": "10.50"}) is True
    assert run(flows.ge("count", 3), {"count": 3}) is True
    assert run(flows.lt("due", date(2026, 12, 1)), {"due": "2026-11-30"}) is True
    assert run(flows.gt("at", datetime(2026, 1, 1, tzinfo=UTC)), {"at": "2026-01-01T03:00:00+03:00"}) is False
    assert run(flows.eq("audience", "public"), {"audience": "public"}) is True
    assert run(flows.ne("audience", "public"), {"audience": "internal"}) is True
    assert run(flows.is_in("audience", ("internal", "public")), {"audience": "public"}) is True
    assert run(flows.truthy("urgent"), {"urgent": True}) is True
    assert run(flows.truthy("urgent"), {}) is False


def test_missing_values_fail_closed():
    assert run(flows.gt("amount", 1), {}) is False
    assert run(flows.lt("amount", 1), {}) is False
    assert run(flows.is_in("audience", ("public",)), {}) is False
    assert run(flows.eq("audience", None), {}) is True
    assert run(flows.ne("audience", None), {"audience": "public"}) is True
    assert run(flows.gt("amount", 1), {"amount": "not-a-number"}) is False


def test_combinators_and_step_outcomes():
    both = flows.all_of(flows.eq(flows.outcome("legal"), "approved"), flows.eq(flows.outcome("editor"), "approved"))
    assert check(both) == []
    assert run(both, outcomes={"legal": "approved", "editor": "approved"}) is True
    assert run(both, outcomes={"legal": "approved", "editor": "rejected"}) is False
    assert run(both, outcomes={"legal": "approved"}) is False
    either = flows.any_of(flows.truthy("urgent"), flows.negate(flows.eq("audience", "public")))
    assert run(either, {"audience": "internal"}) is True
    assert run(either, {"audience": "public"}) is False


def test_registered_conditions_are_called_by_name_only():
    expr = flows.call("example_notes.needs_board")
    assert check(expr) == ["condition 'example_notes.needs_board' is not registered by the flow's app"]
    assert check(expr, frozenset({"example_notes.needs_board"})) == []
    seen = []
    assert run(expr, call=lambda name: seen.append(name) or True) is True
    assert seen == ["example_notes.needs_board"]
    assert run(expr) is False  # No resolver: fail closed.


@pytest.mark.parametrize(
    "expr,problem",
    [
        (Expr("__import__", field="amount", value="os"), "unknown condition operator"),
        (Expr("eval", name="1+1"), "unknown condition operator"),
        (flows.eq("amount.__class__", 1), "undeclared field"),
        (flows.eq("missing", 1), "undeclared field"),
        (flows.eq(flows.outcome("unknown_step"), "approved"), "undeclared field"),
        (flows.gt("amount", "abc"), "is not a decimal"),
        (flows.eq("count", "1.5"), "is not a int"),
        (flows.eq("audience", "elsewhere"), "is not a choice"),
        (flows.gt("urgent", True), "has no order"),
        (flows.gt("reviewer", str(uuid4())), "has no order"),
        (flows.eq("secret", "hunter2"), "sensitive field"),
        (Expr("in", field="audience", value="public"), "needs a list of values"),
        (Expr("all"), "needs its operands"),
        (Expr("not", items=(flows.truthy("urgent"), flows.truthy("urgent"))), "needs its operands"),
        ("amount > 1000", "is not an expression"),
    ],
)
def test_unsafe_or_malformed_conditions_are_refused(expr, problem):
    found = check(expr)
    assert found and problem in found[0]


def test_an_unknown_operator_never_evaluates_to_true():
    assert run(Expr("__import__", field="amount", value="os"), {"amount": "1"}) is False
    assert run(flows.eq("missing", 1), {"missing": 1}) is False


def test_deep_nesting_is_refused():
    expr = flows.truthy("urgent")
    for _ in range(12):
        expr = flows.negate(expr)
    assert "nested too deeply" in check(expr)[0]
    assert run(expr, {"urgent": True}) is False


def test_values_parse_encode_and_decode_by_declared_type():
    person = uuid4()
    samples = {
        "amount": ("10.50", Decimal("10.50"), "10.50"),
        "count": ("7", 7, 7),
        "audience": ("public", "public", "public"),
        "title": ("  Título  ", "Título", "Título"),
        "urgent": ("on", True, True),
        "due": ("2026-11-30", date(2026, 11, 30), "2026-11-30"),
        "at": ("2026-01-01T03:00:00+03:00", datetime(2026, 1, 1, tzinfo=UTC), "2026-01-01T00:00:00+00:00"),
        "reviewer": (str(person), person, str(person)),
    }
    for name, (raw, typed, stored) in samples.items():
        value = parse(FIELDS[name], raw)
        assert value == typed
        assert encode(value) == stored
        assert decode(FIELDS[name], stored) == typed
    assert parse(FIELDS["title"], "   ") is None
    assert parse(FIELDS["urgent"], "") is False


@pytest.mark.parametrize(
    "name,raw",
    [
        ("amount", 1.5),
        ("amount", "NaN"),
        ("count", True),
        ("count", "1e3"),
        ("audience", "elsewhere"),
        ("title", 7),
        ("title", "x" * 501),
        ("urgent", "maybe"),
        ("due", "30/11/2026"),
        ("due", datetime(2026, 1, 1, tzinfo=UTC)),
        ("at", "2026-01-01T00:00:00"),
        ("reviewer", "not-a-uuid"),
    ],
)
def test_values_of_the_wrong_type_are_refused(name, raw):
    with pytest.raises(InvalidValue):
        parse(FIELDS[name], raw)


def test_numeric_bounds():
    bounded = Field("amount", "decimal", minimum="0", maximum="100")
    check_bounds(bounded, Decimal("100"))
    with pytest.raises(InvalidValue):
        check_bounds(bounded, Decimal("-0.01"))
    with pytest.raises(InvalidValue):
        check_bounds(bounded, Decimal("100.01"))
