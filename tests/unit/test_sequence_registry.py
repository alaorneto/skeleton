"""Declared sequences: keys, periods and templates are validated by check E028 (#155)."""

from datetime import UTC, datetime

import pytest

from skeleton.sequences import registry
from skeleton.sequences.registry import SequenceSpec, register_sequence, sequence_problems, template_problem
from skeleton.sequences.services import period_of


@pytest.fixture(autouse=True)
def clean_registry(monkeypatch):
    monkeypatch.setattr(registry, "_sequences", {})
    monkeypatch.setattr(registry, "_duplicates", [])


def test_valid_declarations_have_no_problems():
    register_sequence(SequenceSpec("tickets.number", template="AT-{year}-{number:04d}"))
    register_sequence(SequenceSpec("tickets.monthly", period="month", template="{period}/{number}"))
    assert sequence_problems() == []


@pytest.mark.parametrize(
    "spec,problem",
    [
        (SequenceSpec("Tickets", template="{number}"), "key must look like"),
        (SequenceSpec("tickets.number", period="week"), "period must be one of"),
        (SequenceSpec("tickets.number", template="AT-{year}"), "must contain {number}"),
        (SequenceSpec("tickets.number", template="{number}-{owner}"), "unknown fields"),
        (SequenceSpec("tickets.number", template="{number:Z}"), "invalid template"),
        (SequenceSpec("tickets.number", template="{number"), "invalid template"),
    ],
)
def test_invalid_declarations_are_reported(spec, problem):
    register_sequence(spec)
    assert any(problem in item for item in sequence_problems())


def test_a_conflicting_second_declaration_is_reported():
    register_sequence(SequenceSpec("tickets.number"))
    register_sequence(SequenceSpec("tickets.number", period="month"))
    assert sequence_problems() == ["tickets.number: duplicate registration"]
    assert template_problem("{number:06d}") is None


def test_periods_are_calendar_utc():
    late = datetime(2026, 12, 31, 23, 30, tzinfo=UTC)
    assert period_of(SequenceSpec("a.b", period="year"), late) == "2026"
    assert period_of(SequenceSpec("a.b", period="month"), late) == "2026-12"
    assert period_of(SequenceSpec("a.b", period="none"), late) == ""
