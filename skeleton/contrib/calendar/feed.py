"""Subscription feed (#76): ``/me/apps/skeleton_calendar/feed/<token>.ics``.

Calendar clients send no headers, so the feed token travels in the path; proxies must redact it
from their logs (docs/deployment.md). The token is a feed API token with the ``calendar.feed``
scope; an unknown, expired or revoked token, or one over the rate limit, answers 404. Each
request rereads every organization of the person with the current authorization
(``for_each_organization``), so the feed never outlives a revocation; ``?busy=1`` exports only
busy blocks.
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

from skeleton.service_api.tokens import FeedTokenService, authenticate_feed
from skeleton.tenancy.aggregate import aggregate_route, for_each_organization

if TYPE_CHECKING:
    from uuid import UUID

FEED_SCOPE = "calendar.feed"
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
