"""Sequences are declared in code, like event and notification types (#155)."""

from __future__ import annotations

import re
import string
from dataclasses import dataclass

PERIODS = ("none", "year", "month")
_FIELDS = {"number", "year", "month", "period"}
_KEY = re.compile(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_.]*")


@dataclass(frozen=True)
class SequenceSpec:
    """``template`` is a ``str.format`` pattern over ``number``, ``year``, ``month`` and ``period``.

    ``period="year"`` restarts the numbering each calendar year (UTC), ``"month"`` each month and
    ``"none"`` never. The default template is the bare number.
    """

    key: str
    period: str = "year"
    template: str = "{number}"
    owner_app: str = ""


_sequences: dict[str, SequenceSpec] = {}
_duplicates: list[str] = []


def register_sequence(spec: SequenceSpec) -> SequenceSpec:
    existing = _sequences.get(spec.key)
    if existing is not None and existing != spec:
        _duplicates.append(spec.key)
        return existing
    _sequences[spec.key] = spec
    return spec


def get_sequence(key: str) -> SequenceSpec:
    return _sequences[key]


def all_sequences() -> tuple[SequenceSpec, ...]:
    return tuple(_sequences[key] for key in sorted(_sequences))


def template_problem(template: str) -> str | None:
    try:
        names = {name for _, name, _, _ in string.Formatter().parse(template) if name is not None}
    except ValueError:
        return "invalid template"
    if "number" not in names:
        return "template must contain {number}"
    if names - _FIELDS:
        return f"template uses unknown fields {sorted(names - _FIELDS)}"
    try:
        template.format(number=1, year="2026", month="01", period="2026")
    except (ValueError, KeyError, IndexError):
        return "invalid template"
    return None


def sequence_problems() -> list[str]:
    problems = [f"{key}: duplicate registration" for key in sorted(set(_duplicates))]
    problems.extend(sequence_field_problems())
    for spec in all_sequences():
        if not _KEY.fullmatch(spec.key) or len(spec.key) > 100:
            problems.append(f"{spec.key}: key must look like app_label.name (up to 100 characters)")
        if spec.period not in PERIODS:
            problems.append(f"{spec.key}: period must be one of {', '.join(PERIODS)}")
        problem = template_problem(spec.template)
        if problem:
            problems.append(f"{spec.key}: {problem}")
    return problems


def sequence_field_problems() -> list[str]:
    """``TableSpec.sequence_fields`` name model fields that clients cannot write and declared sequences."""
    from skeleton.system.registry import all_specs

    problems = []
    for spec in all_specs():
        label = spec.model._meta.label_lower
        for item in spec.sequence_fields:
            if not (isinstance(item, tuple) and len(item) == 2):
                problems.append(f"{label}: sequence_fields items are (field, sequence key) pairs")
                continue
            name, key = item
            try:
                spec.model._meta.get_field(name)
            except Exception:  # noqa: BLE001 - FieldDoesNotExist and friends: an unknown field.
                problems.append(f"{label}.{name}: sequence field does not exist")
            if name in spec.write_fields:
                problems.append(f"{label}.{name}: a sequence field cannot be in write_fields")
            if key not in _sequences:
                problems.append(f"{label}.{name}: sequence {key} is not declared")
    return problems
