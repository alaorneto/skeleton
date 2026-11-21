"""The dashboard widget of the Notes example (#620): the five notes changed most recently.

The context authorizes through ``NoteService`` (the app's own service API), in the request's
tenant context: the widget shows only what the person may read, and the core hides it when the
app is disabled or the person has no read permission on the table.
"""

from django.utils.translation import gettext_lazy

from skeleton.system.dashboard_widgets import DashboardWidget, register_dashboard_widget

from .service_api import NoteService


def recent_context(*, request, actor):
    return {"notes": NoteService().recent_notes(actor=actor, limit=5)}


RECENT = DashboardWidget(
    name="example_notes.recent",
    owner_app="example_notes",
    label=gettext_lazy("Anotações recentes"),
    template="example_notes/widgets/recent.html",
    context=recent_context,
    table_key="example_notes.note",
    action="read",
)


def register():
    register_dashboard_widget(RECENT)
