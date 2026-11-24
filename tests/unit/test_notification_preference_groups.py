"""The preferences page groups the notification types by context (#778, #790)."""

from skeleton.notifications.preference_views import group_rows
from skeleton.notifications.registry import NotificationType


def row(code, **kind):
    return {"type_code": code, "label": code, "kind": NotificationType(code, kind.pop("label", code), **kind)}


def test_types_go_to_their_context_and_apps_get_a_card_each():
    rows = [
        {"type_code": "*"},
        row("flows.task_assigned", owner_app="sk_flows", context="organization", label="Tarefa"),
        row("billing.invoice_issued", owner_app="sk_billing", context="platform", label="Fatura"),
        row("example_notes.published", owner_app="example_notes", label="Publicada"),
        row("billing.payment_failed", owner_app="sk_billing", context="platform", label="Pagamento"),
    ]
    default, groups = group_rows(rows)
    assert default == {"type_code": "*"}
    labels = [str(group["label"]) for group in groups]
    assert labels[:2] == ["Da plataforma", "Da organização"] and labels[2].startswith("Dos aplicativos: ")
    assert [item["type_code"] for item in groups[0]["rows"]] == ["billing.invoice_issued", "billing.payment_failed"]
    assert [item["type_code"] for item in groups[-1]["rows"]] == ["example_notes.published"]


def test_without_types_there_are_no_empty_groups():
    assert group_rows([{"type_code": "*"}]) == ({"type_code": "*"}, [])
