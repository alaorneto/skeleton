"""Minha agenda (#75): the person's calendars in every organization, read one organization at a time.

The route is global (``/me/apps/skeleton_calendar/``) and marked ``aggregate_route``: each
organization runs in its own read-only context (``for_each_organization``), so a revoked
TablePermission, an inactive membership or a disabled app drops that organization's items on
the next request. Writing always happens inside one organization: "Novo evento" asks which.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from django.shortcuts import render
from django.urls import reverse

from skeleton.tenancy.aggregate import aggregate_route, for_each_organization

from .service_api import CalendarService
from .views import _zone

APP_LABEL = "skeleton_calendar"


@aggregate_route
def my_agenda(request):
    zone = _zone(request.user)
    try:
        day = date.fromisoformat(request.GET["date"]) if request.GET.get("date") else datetime.now(zone).date()
    except ValueError:
        day = datetime.now(zone).date()
    start = datetime.combine(day, time(), tzinfo=zone)
    end = start + timedelta(days=14)

    def read(organization_id):
        return CalendarService().upcoming(actor=request.user, start=start, end=end)

    results = for_each_organization(
        request.user, read, source="http:calendar-mine", request_id=request.request_id, app_label=APP_LABEL
    )
    items = sorted(
        (
            {
                "organization": row.organization_name,
                "organization_id": row.organization_id,
                "occurrence": occurrence,
                "start": occurrence.starts_at.astimezone(zone),
            }
            for row in results
            for occurrence in row.value
        ),
        key=lambda item: (item["start"], item["organization"]),
    )
    organizations = [
        {
            "name": row.organization_name,
            "new_url": reverse("skeleton_calendar:event-new", kwargs={"organization_id": row.organization_id}),
        }
        for row in results
    ]
    return render(
        request,
        "skeleton_calendar/mine.html",
        {"items": items, "organizations": organizations, "day": day, "zone": str(zone)},
    )
