"""Global search without a database (#581): the marked tables, their order and the request."""

from types import SimpleNamespace

import pytest
from django.core.exceptions import ValidationError
from django.http import QueryDict

from skeleton.records import search
from skeleton.system.registry import GlobalSearchPolicy


def fake_spec(key, *, label, order=None, search_fields=("title",)):
    return SimpleNamespace(
        label=label,
        search_fields=search_fields,
        global_search=GlobalSearchPolicy(order=order) if order is not None else None,
        model=SimpleNamespace(_meta=SimpleNamespace(label_lower=key, verbose_name_plural=key)),
    )


def test_searchable_specs_keep_marked_tables_in_order_then_label_then_key(monkeypatch):
    from skeleton.records import services

    specs = [
        fake_spec("app.unmarked", label="Aaa"),
        fake_spec("app.no_fields", label="Aab", order=1, search_fields=()),
        fake_spec("app.zeta", label="Zeta", order=10),
        fake_spec("app.later", label="Alfa", order=50),
        fake_spec("app.b", label="beta", order=10),
        fake_spec("app.a", label="Beta", order=10),
    ]
    monkeypatch.setattr(services, "readable_specs", lambda user: specs)
    keys = [spec.model._meta.label_lower for spec in search.searchable_specs(object())]
    assert keys == ["app.a", "app.b", "app.zeta", "app.later"]


def test_parse_request_accepts_only_q_and_limit_once(subtests):
    assert search.parse_request(QueryDict("q=contr")) == ("contr", 5)
    assert search.parse_request(QueryDict("q=%20contrato%20%20loc%20&limit=20")) == ("contrato loc", 20)
    for raw in (
        "q=contr&page=1",
        "q=contr&q=loc",
        "q=contr&limit=1&limit=2",
        "limit=5",
        "q=a",
        "q=",
        "q=contr&limit=abc",
    ):
        with subtests.test(raw=raw), pytest.raises(ValidationError):
            search.parse_request(QueryDict(raw))
    for value in ("0", "21", "-1"):
        with subtests.test(limit=value), pytest.raises(ValidationError):
            search.parse_request(QueryDict(f"q=contr&limit={value}"))
    with pytest.raises(ValidationError):
        search.parse_request(QueryDict("q=" + "x" * 201))


def test_limit_refuses_bool_strings_and_out_of_range(subtests):
    assert search._limit(1) == 1 and search._limit(20) == 20
    for value in (True, False, "5", 0, 21, 5.0, None):
        with subtests.test(value=value), pytest.raises(ValidationError):
            search._limit(value)


def test_global_search_declared_reads_only_the_registry(monkeypatch):
    from skeleton.system import registry

    monkeypatch.setattr(registry, "all_specs", lambda: [fake_spec("app.a", label="A")])
    assert search.global_search_declared() is False
    monkeypatch.setattr(registry, "all_specs", lambda: [fake_spec("app.a", label="A", order=0)])
    assert search.global_search_declared() is True
