from copy import deepcopy

import pytest

from deployment_agent.releases import presentation_diff
from deployment_agent.storage import digest


def inventory(*, state="installed", fields=(), screens=(), sections=(), layouts=()):
    return {"apps": [{"state": state, "manifest": {
        "app_id": "org.test.presentation", "app_label": "presentation",
        "runtime": {"configuration_fields": list(fields),
                    "administration": {"sections": list(sections), "screens": list(screens)},
                    "model_ui": list(layouts)},
    }}]}


def test_preview_identifies_incompatible_configuration_and_removed_screen():
    before = inventory(fields=[{"name": "caption", "type": "str", "default": "", "max_length": 200}],
                       screens=[{"id": "old", "label": "Old screen"}])
    after = inventory(fields=[{"name": "caption", "type": "str", "default": "", "max_length": 100}],
                      screens=[{"id": "new", "label": "New screen"}],
                      layouts=[{"table_key": "presentation.item", "list": {"columns": ["code"]}}])
    changes = presentation_diff(before, after)
    assert {(row["resource"], row["id"], row["action"]) for row in changes} == {
        ("configuration", "caption", "change"), ("screen", "old", "remove"),
        ("screen", "new", "add"), ("model_layout", "presentation.item", "add"),
    }
    assert [row["id"] for row in changes if row["incompatible"]] == ["caption"]


def test_retirement_removes_executable_group_but_reinstall_restores_it():
    active = inventory(screens=[{"id": "screen", "label": "Screen"}])
    retained = inventory(state="schema_retained", screens=[{"id": "screen", "label": "Screen"}])
    assert {(row["resource"], row["action"]) for row in presentation_diff(active, retained)} == {
        ("group", "remove"), ("screen", "remove"),
    }
    assert {(row["resource"], row["action"]) for row in presentation_diff(retained, active)} == {
        ("group", "add"), ("screen", "add"),
    }


@pytest.mark.parametrize('dimension, before_value, after_value', [
    ('col_span', 4, 8), ('rows', 8, 12), ('col_span', None, 12), ('rows', None, 8),
])
def test_dimension_only_layout_change_is_reviewable_and_changes_plan_digest(dimension, before_value, after_value):
    layout = {'table_key': 'presentation.item', 'form': {'sections': [
        {'id': 'content', 'label': 'Content', 'fields': [
            {'name': 'body', 'widget': 'textarea', dimension: before_value}]}]}}
    changed = deepcopy(layout)
    changed['form']['sections'][0]['fields'][0][dimension] = after_value
    before = inventory(layouts=[layout])
    after = inventory(layouts=[changed])
    snapshot = deepcopy((before, after))
    changes = presentation_diff(before, after)
    assert changes == [{
        'app_id': 'org.test.presentation', 'app_label': 'presentation',
        'resource': 'model_layout', 'id': 'presentation.item', 'action': 'change',
        'before': layout, 'after': changed, 'incompatible': False,
    }]
    unchanged_plan = {'presentation_changes': presentation_diff(before, before)}
    changed_plan = {'presentation_changes': changes}
    assert digest(changed_plan) != digest(unchanged_plan)
    another = deepcopy(changed_plan)
    another['presentation_changes'][0]['after']['form']['sections'][0]['fields'][0][dimension] = 6
    assert digest(another) != digest(changed_plan)
    assert (before, after) == snapshot
