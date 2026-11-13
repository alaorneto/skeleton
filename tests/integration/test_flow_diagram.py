"""The diagram of the flow on the instance page (#419)."""

import pytest
from django.urls import reverse

from skeleton.flows.diagram import layout
from skeleton.testing.service_api import FIXTURE_PASSWORD
from tests import support
from tests.integration.test_flow_service_api import approval_spec, scenario, start

pytestmark = pytest.mark.needs_example_app
__all__ = ["scenario"]


def test_the_layout_puts_steps_in_columns_by_distance_and_marks_states():
    drawn = layout(approval_spec(), active={"review"}, done={"start", "fill"})
    columns = {node["key"]: node["x"] for node in drawn["nodes"]}
    assert columns["start"] < columns["fill"] < columns["review"] < columns["confirm"] < columns["done"]
    states = {node["key"]: node["state"] for node in drawn["nodes"]}
    assert (states["review"], states["fill"], states["done"]) == ("active", "done", "idle")
    back = [edge for edge in drawn["edges"] if edge["back"]]
    assert len(back) == 1  # review -> fill on "rejected" goes back.
    assert drawn["width"] > 0 and drawn["height"] > 0


def test_the_instance_page_draws_the_diagram_with_the_current_step(scenario, monkeypatch):
    from dataclasses import replace

    from skeleton.flows import registry

    unsafe = replace(
        approval_spec(),
        steps=tuple(
            replace(step, label="<b>Preencher</b>") if step.key == "fill" else step for step in approval_spec().steps
        ),
        _index={},
    )
    monkeypatch.setitem(registry._flows, unsafe.key, unsafe)
    receipt = start(scenario)
    page = support.login(scenario.member, password=FIXTURE_PASSWORD, csrf=False).get(
        reverse("flows:instance", args=[scenario.a.pk, receipt.sysid])
    )
    body = page.content.decode()
    assert page.status_code == 200 and "data-flow-diagram" in body
    assert 'data-step="fill" data-state="active"' in body
    assert "&lt;b&gt;Preencher&lt;/b&gt;" in body and "<b>Preencher</b>" not in body
    assert "<script" not in body.split("data-flow-diagram", 1)[1].split("</svg>", 1)[0]
