"""Subscription feed (#76): ``/me/apps/skeleton_calendar/feed/<token>.ics``.

Calendar clients send no headers, so the feed token travels in the path; proxies must redact it
from their logs (docs/deployment.md). The token is a feed API token with the ``calendar.feed``
scope; an unknown, expired or revoked token, or one over the rate limit, answers 404. Each
request rereads every organization of the person with the current authorization
(``for_each_organization``), so the feed never outlives a revocation; ``?busy=1`` exports only
busy blocks.

The link of one calendar (#669), ``/me/apps/skeleton_calendar/calendar-feed/<token>.ics``, carries a
feed token of the ``calendar.link`` scope bound to that calendar (``FeedTokenService.issue_for_record``,
from the calendar's link page). Each request opens only the calendar's organization, rereads
whether the person still reads the calendar and exports it with the same masking; an unreadable
calendar answers 404 like an unknown token, and reading it again brings the link back.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from django.contrib import messages
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.translation import gettext
from django.views.decorators.http import require_http_methods, require_safe

from skeleton.service_api.tokens import FeedTokenService, authenticate_feed, authenticate_record_feed
from skeleton.tenancy.aggregate import aggregate_route, for_each_organization

if TYPE_CHECKING:
    from uuid import UUID

FEED_SCOPE = "calendar.feed"
LINK_SCOPE = "calendar.link"  # A feed token bound to one calendar (#669).
TABLE_KEY = "skeleton_calendar.calendar"
APP_LABEL = "skeleton_calendar"
FEED_PAST = timedelta(days=30)  # Events that ended longer ago leave the feed (#291).
FEED_FUTURE = timedelta(days=400)  # Single events that start later are not exported yet.


@aggregate_route
@require_safe
def feed(request: HttpRequest, token: str) -> HttpResponse:
    from django.db.models import Q

    from .ics import calendar_bytes, calendar_components, capped
    from .policies import readable_events

    user = authenticate_feed(request, token, scope=FEED_SCOPE)
    if user is None:
        raise Http404
    busy_only = request.GET.get("busy") == "1"
    now = timezone.now()
    recent = Q(recurrence_end__gte=now - FEED_PAST) | Q(recurrence_end__isnull=True)
    soon = ~Q(rrule="") | Q(starts_at__lt=now + FEED_FUTURE) | Q(start_date__lte=(now + FEED_FUTURE).date())

    def read(organization_id: UUID) -> Any:
        return calendar_components(user, readable_events(user).filter(recent).filter(soon), busy_only=busy_only)

    results = for_each_organization(user, read, source="http:calendar-feed", app_label=APP_LABEL)
    body = calendar_bytes(gettext("Agenda"), capped([component for row in results for component in row.value]))
    response = HttpResponse(body, content_type="text/calendar; charset=utf-8")
    response["Cache-Control"] = "no-store"
    return response


feed.skeleton_token_route = True  # type: ignore[attr-defined]


def _window() -> Any:
    from django.db.models import Q

    now = timezone.now()
    recent = Q(recurrence_end__gte=now - FEED_PAST) | Q(recurrence_end__isnull=True)
    soon = ~Q(rrule="") | Q(starts_at__lt=now + FEED_FUTURE) | Q(start_date__lte=(now + FEED_FUTURE).date())
    return recent & soon


@aggregate_route
@require_safe
def calendar_feed(request: HttpRequest, token: str) -> HttpResponse:
    """The subscription of one calendar (#669): only that calendar, reread on every request."""
    from .ics import calendar_bytes, calendar_components, capped
    from .policies import readable_calendars, readable_events

    bound = authenticate_record_feed(request, token, scope=LINK_SCOPE, table_key=TABLE_KEY)
    if bound is None:
        raise Http404
    user, busy_only = bound.user, request.GET.get("busy") == "1"

    def read(organization_id: UUID) -> Any:
        calendar = readable_calendars(user).filter(pk=bound.record_id).first()
        if calendar is None:
            return None  # Not readable any more: the same 404 as an unknown token.
        events = readable_events(user).filter(calendar=calendar).filter(_window())
        return calendar.name, calendar_components(user, events, busy_only=busy_only)

    results = for_each_organization(
        user,
        read,
        source="http:calendar-link",
        request_id=getattr(request, "request_id", None),
        app_label=APP_LABEL,
        limit=1,
        organization_id=bound.organization_id,
    )
    value = results[0].value if results else None
    if value is None:
        raise Http404
    name, components = value
    response = HttpResponse(calendar_bytes(name, capped(components)), content_type="text/calendar; charset=utf-8")
    response["Cache-Control"] = "no-store"
    return response


calendar_feed.skeleton_token_route = True  # type: ignore[attr-defined]


def issue_calendar_link(request: Any, calendar: Any) -> str:
    """Issue the person's link to ``calendar`` (session, POST + CSRF) and return its address, shown once."""
    from django.urls import reverse

    issued = FeedTokenService().issue_for_record(
        actor=request.user,
        name=(gettext("Calendário: %(name)s") % {"name": calendar.name})[:150],
        scope=LINK_SCOPE,
        expires_at=timezone.now() + timedelta(days=365),
        table_key=TABLE_KEY,
        record_id=calendar.pk,
    )
    return request.build_absolute_uri(reverse("skeleton_calendar_me:calendar-feed", kwargs={"token": issued.text}))


@aggregate_route
@require_http_methods(["GET", "POST"])
# Any: aggregate_route admits only an authenticated person, and the middleware sets request_id.
def feed_token(request: Any) -> HttpResponse:
    """Issue a feed token (session, POST + CSRF); its URL is shown once."""
    from skeleton.service_api import Forbidden, InvalidInput
    from skeleton.tenancy.aggregate import user_context

    url = None
    if request.method == "POST":
        with user_context(request, source="http:calendar-feed-token"):
            try:
                issued = FeedTokenService().issue(
                    actor=request.user,
                    name="Feed da agenda",
                    scope=FEED_SCOPE,
                    expires_at=timezone.now() + timedelta(days=365),
                )
            except (Forbidden, InvalidInput):
                messages.error(request, gettext("Não foi possível criar o feed."))
            else:
                url = request.build_absolute_uri(f"/me/apps/{APP_LABEL}/feed/{issued.text}.ics")
    response = render(request, "skeleton_calendar/feed_token.html", {"url": url}, status=201 if url else 200)
    response["Cache-Control"] = "no-store"
    return response


def register_feed_scope() -> None:
    from skeleton.integrations.tokens.scopes import register_token_scope

    register_token_scope(FEED_SCOPE, gettext("Assinatura da agenda por um cliente de calendário"), feed=True)
    register_token_scope(LINK_SCOPE, gettext("Assinatura de um calendário por um cliente de calendário"), feed=True)
