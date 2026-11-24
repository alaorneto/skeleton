"""The text of a notification: closed grammar, validation, rendering and formatting (#782)."""

from datetime import UTC, datetime

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone, translation

from skeleton.notifications.registry import NotificationField
from skeleton.notifications.texts import (
    FIXED,
    date_text,
    first_name,
    fixed_samples,
    format_value,
    parse,
    placeholder_list,
    placeholders,
    render,
    render_html,
    sample_values,
    validate,
)

ALLOWED = frozenset({"number", "title", "organizacao"})


def code_of(text, *, kind="title", allowed=ALLOWED):
    with pytest.raises(ValidationError) as caught:
        validate(text, allowed=allowed, kind=kind)
    return caught.value.code


def test_placeholders_and_literal_braces():
    assert placeholders("Fatura {number} de {organizacao}") == {"number", "organizacao"}
    assert render("Fatura {number}", {"number": "F-1"}) == "Fatura F-1"
    assert render("{{x}} e {{ y }}", {}) == "{x} e { y }"
    assert render("Olá {title}!", {}) == "Olá !"
    assert [segment.name for segment in parse("a{title}b") if segment.name] == ["title"]


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("{x.__class__}", "invalid_placeholder"),
        ("{0}", "invalid_placeholder"),
        ("{Title}", "invalid_placeholder"),
        ("{title!r}", "invalid_placeholder"),
        ("{title:>10}", "invalid_placeholder"),
        ("{ab", "unbalanced_brace"),
        ("}", "unbalanced_brace"),
        ("a { b", "unbalanced_brace"),
    ],
)
def test_unsafe_or_broken_placeholders_are_refused(text, code):
    assert code_of(text) == code


def test_validate_limits_and_unknown_placeholders():
    assert validate("  Fatura {number}  ", allowed=ALLOWED, kind="title") == "Fatura {number}"
    assert code_of("") == "empty"
    assert code_of("linha\noutra") == "multiline"
    assert code_of("x" * 151) == "too_long"
    assert code_of("Olá {pessoa}") == "unknown_placeholder"
    assert code_of("tab\there") == "control_char"
    assert validate("um\r\ndois", allowed=ALLOWED, kind="body") == "um\ndois"
    assert code_of("x" * 2001, kind="body") == "too_long"
    assert code_of("\n".join(["l"] * 31), kind="body") == "too_long"
    assert code_of("bell\x07", kind="body") == "control_char"
    assert validate("", allowed=ALLOWED, kind="body") == ""


def test_render_html_escapes_values_and_text_and_breaks_lines():
    html = render_html("<b>{title}</b>\nfim", {"title": "<script>alert(1)</script>"})
    assert "<script>" not in html and "<b>" not in html
    assert "&lt;script&gt;" in html and "&lt;b&gt;" in html
    assert html.endswith("<br>fim")


def test_format_value_by_kind():
    values = {"currency": "BRL", "jpy": "JPY"}
    with translation.override("pt-br"):
        assert (
            format_value(
                NotificationField("total", "Valor", kind="money", currency_field="currency"), 123456, values=values
            )
            == "1.234,56 BRL"
        )
        assert (
            format_value(NotificationField("total", "Valor", kind="money", currency_field="jpy"), 500, values=values)
            == "500 JPY"
        )
        assert format_value(NotificationField("due", "Vence", kind="date"), "2026-11-15", values={}) == "15/11/2026"
        assert format_value(NotificationField("due", "Vence", kind="date"), "não é data", values={}) == "não é data"
        assert format_value(NotificationField("n", "N", kind="number"), 12345, values={}) == "12.345"
        assert format_value(NotificationField("p", "P", kind="person"), "u1", values={}, people={"u1": "Ana"}) == "Ana"
        assert format_value(NotificationField("p", "P", kind="person"), "u2", values={}) == "Pessoa indisponível"
        assert format_value(NotificationField("t", "T"), None, values={}) == ""
        bad_money = NotificationField("total", "Valor", kind="money", currency_field="currency")
        assert format_value(bad_money, "abc", values=values) == "abc"


def test_sample_values_cover_every_kind():
    fields = tuple(NotificationField(kind, kind, kind=kind) for kind in ("text", "number", "date", "money", "person"))
    with translation.override("pt-br"):
        samples = sample_values(fields)
    assert samples["number"] == "42" and samples["money"] == "1.234,56 BRL"
    assert samples["date"] == timezone.localdate().strftime("%d/%m/%Y")


def test_fixed_placeholders_of_the_recipient_and_of_general_use():
    assert set(FIXED) == {
        "organizacao",
        "aviso",
        "plataforma",
        "data_atual",
        "destinatario",
        "destinatario_primeiro_nome",
    }
    assert first_name("  Maria  da Silva ") == "Maria" and first_name("") == ""
    with translation.override("pt-br"):
        samples = fixed_samples(organization="Org")
        moment = datetime(2026, 11, 24, 2, 30, tzinfo=UTC)
        with timezone.override("America/Sao_Paulo"):
            assert date_text(moment) == "23/11/2026"  # The recipient's time zone, not UTC.
        with timezone.override("UTC"):
            assert date_text(moment) == "24/11/2026"
    assert samples["destinatario"] == "Maria Exemplo" and samples["destinatario_primeiro_nome"] == "Maria"
    assert samples["organizacao"] == "Org" and samples["plataforma"]
    assert samples["data_atual"] == timezone.localdate().strftime("%d/%m/%Y")


def test_the_marker_list_shows_the_fields_then_the_fixed_placeholders():
    from skeleton.notifications.registry import NotificationType

    kind = NotificationType("t.x", "X", fields=(NotificationField("due", "Prazo", kind="date"),))
    names = [item["placeholder"] for item in placeholder_list(kind)]
    assert names[0] == "{due}"
    assert {name.strip("{}") for name in names[1:]} == set(FIXED)
