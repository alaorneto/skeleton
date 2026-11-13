"""Form data retention (#422): which closed instances are past their deadline, canonical form."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from skeleton.flows import registry as flows
from skeleton.flows.retention import expired, retention_days
from skeleton.flows.spec import FlowSpec
from skeleton.flows.validation import structural_problems

NOW = datetime(2026, 11, 13, 12, tzinfo=UTC)


def spec(**changes):
    base = flows.FlowSpec(
        key="demo.retained",
        version=1,
        label="Retido",
        owner_app="demo",
        subject_table="demo.item",
        steps=(flows.start("start"), flows.end("done")),
        transitions=(flows.Transition("start", "done"),),
        start_form=flows.Form(flows.Field("phone", personal=True), flows.Field("note")),
    )
    return replace(base, **changes)


def test_selection_by_the_deadline(settings):
    settings.SKELETON_FLOWS_FORM_RETENTION_DAYS = 0
    keep = SimpleNamespace(retention_days=0)
    month = SimpleNamespace(retention_days=30)
    assert not expired(keep, NOW - timedelta(days=9999), now=NOW)
    assert expired(month, NOW - timedelta(days=30), now=NOW)
    assert not expired(month, NOW - timedelta(days=29, hours=23), now=NOW)
    assert not expired(month, None, now=NOW)
    settings.SKELETON_FLOWS_FORM_RETENTION_DAYS = 7
    assert retention_days(keep) == 7 and retention_days(month) == 30
    assert expired(keep, NOW - timedelta(days=8), now=NOW)


def test_defaults_stay_out_of_the_canonical_form():
    plain = spec().to_json()
    assert "retention_days" not in plain
    assert [item.get("personal") for item in plain["start_form"]["fields"]] == [True, None]
    declared = spec(retention_days=30)
    again = FlowSpec.from_json(declared.to_json())
    assert again.retention_days == 30 and again.digest() == declared.digest()
    assert again.data_fields()["phone"].personal


def test_validation_of_retention_and_personal():
    assert structural_problems(spec()) == []
    assert any("retention_days" in item for item in structural_problems(spec(retention_days=-1)))
    assert any("retention_days" in item for item in structural_problems(spec(retention_days=True)))
    odd = spec(start_form=flows.Form(flows.Field("phone", personal="yes")))
    assert any("personal" in item for item in structural_problems(odd))
