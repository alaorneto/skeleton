"""The preferences page groups the notification types by area (#778)."""

from skeleton.notifications.preference_views import AREAS, group_rows


def test_types_go_to_their_area_and_unknown_prefixes_to_others():
    rows = [
        {"type_code": "*"},
        {"type_code": "flows.task_assigned"},
        {"type_code": "billing.invoice_issued"},
        {"type_code": "example_notes.published"},
        {"type_code": "billing.payment_failed"},
    ]
    default, groups = group_rows(rows)
    assert default == {"type_code": "*"}
    labels = dict(AREAS)
    assert [str(group["label"]) for group in groups] == [str(labels["billing"]), str(labels["flows"]), "Outros"]
    assert [row["type_code"] for row in groups[0]["rows"]] == ["billing.invoice_issued", "billing.payment_failed"]
    assert [row["type_code"] for row in groups[-1]["rows"]] == ["example_notes.published"]


def test_without_types_there_are_no_empty_groups():
    assert group_rows([{"type_code": "*"}]) == ({"type_code": "*"}, [])
