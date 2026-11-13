"""Minha agenda (#75): the person's calendars in every organization, read one organization at a time.

The route is global (``/me/apps/skeleton_calendar/``) and marked ``aggregate_route``: each
organization runs in its own read-only context (``for_each_organization``), so a revoked
TablePermission, an inactive membership or a disabled app drops that organization's items on
the next request. Writing always happens inside one organization: "Novo evento" asks which.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse

from skeleton.tenancy.aggregate import aggregate_route, for_each_organization, user_context

from .service_api import CalendarService
from .views import _zone

if TYPE_CHECKING:
    from uuid import UUID

APP_LABEL = "skeleton_calendar"


@aggregate_route
# Any: aggregate_route admits only an authenticated person, and the middleware sets request_id.
def my_agenda(request: Any) -> HttpResponse:
    with user_context(request, source="http:calendar-mine-zone"):  # The profile zone needs the person's context.
        zone = _zone(request.user)
    try:
        day = date.fromisoformat(request.GET["date"]) if request.GET.get("date") else datetime.now(zone).date()
    except ValueError:
        day = datetime.now(zone).date()
    start = datetime.combine(day, time(), tzinfo=zone)
    end = start + timedelta(days=14)

    def read(organization_id: UUID) -> Any:
        from skeleton.service_api import InvalidInput

        try:
            return CalendarService().upcoming(actor=request.user, start=start, end=end)
        except InvalidInput:
            return None  # Too many occurrences here: listed apart, never fatal for the page (#291).

    results = for_each_organization(
        request.user, read, source="http:calendar-mine", request_id=request.request_id, app_label=APP_LABEL
    )
    last_day = day + timedelta(days=14)
    items = sorted(
        (
            {
                "organization": row.organization_name,
                "organization_id": row.organization_id,
                "occurrence": occurrence,
                # An all-day event belongs to dates, not instants: it sorts at the local midnight (#312).
                "start": datetime.combine(occurrence.start_date, time(), tzinfo=zone)
                if occurrence.all_day
                else occurrence.starts_at.astimezone(zone),
            }
            for row in results
            if row.value is not None
            for occurrence in row.value
            # The UTC window of the local days overlaps the neighbouring all-day dates: keep the local ones.
            if not occurrence.all_day or (occurrence.start_date < last_day and occurrence.end_date > day)
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
    from django.utils import timezone

    with timezone.override(zone):  # Times render in the profile zone, not the default one (#312).
        return render(
            request,
            "skeleton_calendar/mine.html",
            {
                "items": items,
                "organizations": organizations,
                "day": day,
                "zone": str(zone),
                "skipped": [row.organization_name for row in results if row.value is None],
            },
        )
