"""Dashboard widgets declared by apps (#620), on PostgreSQL with the runtime role.

Who sees a widget (effective app, table gate), what it shows (only what the actor reads, never
another organization) and what happens when the app fails (the neutral box, never a 500).
"""

import logging

import pytest
from django.db import connection
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext

from skeleton.system import dashboard_widgets
from skeleton.system.dashboard_widgets import DashboardWidget, get_dashboard_widget, visible_widgets
from skeleton.testing.service_api import acting_as, build_service_scenario

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]
RECENT = "example_notes.recent"
UNAVAILABLE = "Este quadro não está disponível agora."


@pytest.fixture
def scenario():
    from examples.notes.models import Note

    return build_service_scenario(Note, slug="widgets")


@pytest.fixture
def extra_widgets(monkeypatch):
    """Extra registrations for one test; the real registry comes back at the end."""
    registry = dict(dashboard_widgets._registry)
    monkeypatch.setattr(dashboard_widgets, "_registry", registry)
    return registry


def names(actor):
    return [widget.name for widget in visible_widgets(actor)]


def request_for(actor):
    request = RequestFactory().get("/")
    request.user = actor
    return request


def test_only_effective_apps_and_passed_gates_show_widgets(scenario, subtests):
    with subtests.test("member with read permission"), acting_as(scenario.member, scenario.a) as actor:
        assert RECENT in names(actor)
    with subtests.test("member without table permission"), acting_as(scenario.denied, scenario.a) as actor:
        assert RECENT not in names(actor)
    with subtests.test("app disabled"), scenario.application_disabled():
        with acting_as(scenario.member, scenario.a) as actor:
            assert RECENT not in names(actor)
    with subtests.test("disabled in B only"), scenario.application_disabled(scenario.b):
        with acting_as(scenario.member, scenario.a) as actor:
            assert RECENT in names(actor)
        with acting_as(scenario.member, scenario.b) as actor:
            assert RECENT not in names(actor)


def test_capability_gate(scenario, extra_widgets):
    gated = DashboardWidget(
        "example_notes.admin_box",
        "example_notes",
        "Admin",
        "example_notes/widgets/recent.html",
        lambda **kwargs: {},
        capability="apps.manage",
    )
    extra_widgets[gated.name] = gated
    with acting_as(scenario.admin_a, scenario.a) as actor:
        assert gated.name in names(actor)
    with acting_as(scenario.member, scenario.a) as actor:
        assert gated.name not in names(actor)


def test_visible_widgets_reads_permissions_once_whatever_the_number_of_widgets(scenario, extra_widgets):
    base = get_dashboard_widget(RECENT)
    with acting_as(scenario.member, scenario.a) as actor:
        visible_widgets(actor)  # Warm the per-request caches of the context (app states).
        with CaptureQueriesContext(connection) as one:
            visible_widgets(actor)
        for index in range(8):
            name = f"example_notes.copy_{index}"
            extra_widgets[name] = DashboardWidget(
                name, "example_notes", "x", base.template, base.context, base.table_key
            )
        with CaptureQueriesContext(connection) as nine:
            assert len(visible_widgets(actor)) >= 9
    assert len(one) == len(nine)
    assert sum("access_tablepermission" in query["sql"] for query in nine.captured_queries) <= 1


def test_the_widget_lists_only_what_the_actor_reads_in_this_organization(scenario):
    from skeleton.web.widgets import render_widget

    scenario.create(scenario.a, code="a-shared", title="Nota de A")
    scenario.create(scenario.a, owner=scenario.denied, visibility="private", code="a-private", title="Privada de A")
    scenario.create(scenario.b, code="b-shared", title="Nota de B")
    widget = get_dashboard_widget(RECENT)
    with acting_as(scenario.member, scenario.a) as actor:
        html = str(render_widget(request_for(actor), widget))
    assert "Nota de A" in html
    assert "Privada de A" not in html and "Nota de B" not in html and UNAVAILABLE not in html
    with acting_as(scenario.member, scenario.b) as actor:
        html = str(render_widget(request_for(actor), widget))
    assert "Nota de B" in html and "Nota de A" not in html


def test_an_error_of_the_app_becomes_the_neutral_box_and_is_logged(scenario, extra_widgets, caplog, subtests):
    from django.db import connection as db

    from skeleton.service_api import Forbidden
    from skeleton.web.widgets import render_widget

    def broken(**kwargs):
        raise RuntimeError("secret detail of the app")

    def bad_query(**kwargs):
        with db.cursor() as cursor:
            cursor.execute("SELECT * FROM table_that_does_not_exist")
        return {}

    def refused(**kwargs):
        raise Forbidden()

    template = "example_notes/widgets/recent.html"
    cases = {"exception": (broken, True), "database error": (bad_query, True), "service refusal": (refused, False)}
    for name, (context, logged) in cases.items():
        with subtests.test(name):
            widget = DashboardWidget(f"example_notes.{name.replace(' ', '_')}", "example_notes", "x", template, context)
            caplog.clear()
            with caplog.at_level(logging.ERROR, logger="skeleton.security"):
                with acting_as(scenario.member, scenario.a) as actor:
                    html = str(render_widget(request_for(actor), widget))
                    # The transaction of the page survives the app's failed query (savepoint).
                    assert scenario.model.objects.count() >= 0
            assert UNAVAILABLE in html and "secret detail" not in html
            records = [record for record in caplog.records if getattr(record, "event", "") == "dashboard.widget_failed"]
            assert bool(records) is logged
            if logged:
                assert records[0].reason_code == widget.name and "secret detail" not in records[0].getMessage()
