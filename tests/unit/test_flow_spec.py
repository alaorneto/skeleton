"""Flow specifications are pure data with a canonical JSON form and a stable digest (#201)."""

import dataclasses
import json
from datetime import timedelta
from decimal import Decimal

import pytest

from skeleton.flows import registry as flows
from skeleton.flows.spec import FlowSpec
from skeleton.flows.validation import structural_problems
from tools.flow_fixtures import review_spec, review_steps, review_transitions


def test_the_sample_flow_is_structurally_valid():
    assert structural_problems(review_spec()) == []


def test_json_round_trip_keeps_the_specification_and_the_digest():
    spec = review_spec(
        start_form=flows.Form(flows.Field("origin", "str")),
        subject_events=(("completed", "example_notes.note_reviewed"),),
        number_sequence="example_notes.review_number",
    )
    stored = json.loads(json.dumps(spec.to_json()))  # What a JSONField gives back.
    restored = FlowSpec.from_json(stored)
    assert restored == spec
    assert restored.digest() == spec.digest()
    assert restored.canonical() == spec.canonical()
    assert structural_problems(restored) == []


def test_the_digest_is_stable_and_changes_with_any_change():
    assert review_spec().digest() == review_spec().digest()
    assert len(review_spec().digest()) == 64
    steps = review_steps()
    steps["cool"] = dataclasses.replace(steps["cool"], delay_seconds=61)
    transitions = review_transitions()
    transitions[1] = dataclasses.replace(transitions[1], label="Submeter")
    digests = {
        review_spec().digest(),
        review_spec(steps=steps).digest(),
        review_spec(transitions=transitions).digest(),
        review_spec(label="Outro rótulo").digest(),
        review_spec(version=2).digest(),
    }
    assert len(digests) == 5


def test_the_canonical_form_holds_only_json_scalars():
    def walk(value):
        if isinstance(value, dict):
            assert all(isinstance(key, str) for key in value)
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        else:
            assert value is None or type(value) in (str, int, bool)

    walk(review_spec().to_json())
    assert "_index" not in review_spec().to_json()


def test_builders_normalize_durations_literals_and_bounds():
    assert flows.timer("t", delay_seconds=timedelta(minutes=2)).delay_seconds == 120
    sla = flows.Sla(due_seconds=timedelta(days=2), remind_before_seconds=(timedelta(hours=1),))
    assert (sla.due_seconds, sla.remind_before_seconds) == (172800, (3600,))
    assert flows.gt("amount", Decimal("10.50")).value == "10.50"
    assert flows.is_in("audience", ["internal", "public"]).value == ("internal", "public")
    assert flows.Field("amount", "decimal", minimum=0, maximum=Decimal("9.5")).minimum == "0"
    assert flows.outcome("legal") == "@outcome.legal"
    with pytest.raises(TypeError):
        flows.gt("amount", 1.5)  # Floats are refused: compare decimals as text or Decimal.
    with pytest.raises(TypeError):
        flows.timer("t", delay_seconds="60")


def test_lookups_and_transition_identity():
    spec = review_spec()
    assert spec.start_step().key == "start"
    assert [item.target for item in spec.outgoing("fork")] == ["legal", "editor"]
    assert {item.source for item in spec.incoming("prepare")} == {"start", "single", "verdict"}
    assert spec.outgoing("prepare")[0].ident == "submit"
    assert spec.outgoing("legal")[1].ident == "legal>joined:rejected"
    assert set(spec.data_fields()) == {"audience", "amount", "remark", "reviewer", "notify", "reason", "reference"}
    assert spec.data_fields()["remark"].sensitive
