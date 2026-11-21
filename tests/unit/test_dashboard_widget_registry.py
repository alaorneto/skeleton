"""Registration of dashboard widgets (#620): names, prefixes and duplicates, without a database."""

import pytest
from django.core.exceptions import ImproperlyConfigured

from skeleton.system import dashboard_widgets
from skeleton.system.dashboard_widgets import (
    DashboardWidget,
    get_dashboard_widget,
    register_dashboard_widget,
)
from skeleton.system.dashboard_widgets import (
    dashboard_widgets as all_widgets,
)


def context(*, request, actor):
    return {}


def widget(name="test_app.box", owner="test_app", **values):
    values = {"label": "Box", "template": "t.html", "context": context, **values}
    return DashboardWidget(name=name, owner_app=owner, **values)


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setattr(dashboard_widgets, "_registry", {})
    return dashboard_widgets._registry


def test_register_get_and_list(registry):
    first = register_dashboard_widget(widget())
    register_dashboard_widget(widget("test_app.another_box"))
    assert get_dashboard_widget("test_app.box") is first
    assert get_dashboard_widget("test_app.missing") is None
    assert [item.name for item in all_widgets()] == ["test_app.another_box", "test_app.box"]
    assert register_dashboard_widget(widget()) is first  # The same declaration loaded again changes nothing.


def test_invalid_names_prefixes_and_duplicates_are_refused(registry, subtests):
    register_dashboard_widget(widget())
    cases = {
        "repeated name": widget(template="other.html"),
        "prefix of another app": widget("other_app.box"),
        "no slug": widget("test_app"),
        "slug with dash": widget("test_app.my-box"),
        "slug with uppercase": widget("test_app.Box"),
        "slug too long": widget("test_app." + "x" * 41),
        "empty name": widget(""),
    }
    for name, item in cases.items():
        with subtests.test(name), pytest.raises(ImproperlyConfigured):
            register_dashboard_widget(item)
    assert list(registry) == ["test_app.box"]


def test_check_e045_accepts_valid_widgets_and_reports_each_problem(registry, subtests):
    from skeleton.system.checks.widgets import dashboard_widget_checks

    good = DashboardWidget(
        name="sk_flows.box",
        owner_app="sk_flows",
        label="Box",
        template="skeleton/web/pages/widget-unavailable.html",
        context=context,
        table_key="sk_tenancy.organizationsettings",
        action="read",
    )
    register_dashboard_widget(good)
    assert dashboard_widget_checks() == []
    cases = {
        "unknown owner app": (widget("nope_app.box", "nope_app", template=good.template), "owner_app"),
        "unknown table": (
            DashboardWidget("sk_flows.t", "sk_flows", "x", good.template, context, table_key="nope.table"),
            "table_key",
        ),
        "invalid action": (
            DashboardWidget("sk_flows.a", "sk_flows", "x", good.template, context, action="drop"),
            "action",
        ),
        "missing template": (DashboardWidget("sk_flows.m", "sk_flows", "x", "nope/missing.html", context), "template"),
        "context not callable": (DashboardWidget("sk_flows.c", "sk_flows", "x", good.template, {"a": 1}), "context"),
    }
    for name, (item, word) in cases.items():
        with subtests.test(name):
            registry.clear()
            register_dashboard_widget(item)
            errors = dashboard_widget_checks()
            assert [error.id for error in errors] == ["skeleton.E045"]
            assert word in errors[0].msg


def test_the_registration_surface_is_public_and_the_rest_is_not():
    from skeleton.system.import_boundary import classify_import

    def classify(name):
        found = classify_import(
            "skeleton.system.dashboard_widgets",
            f"skeleton.system.dashboard_widgets.{name}",
            own_packages=("applications.notes",),
            providers=[],
            dependencies=[],
        )
        return found[0] if found else None

    assert classify("DashboardWidget") is None and classify("register_dashboard_widget") is None
    assert classify("visible_widgets") == "core_import"  # W019: only the core decides who sees a widget.
