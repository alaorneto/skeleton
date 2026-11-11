"""Organization pages of the calendar under /o/<org>/apps/skeleton_calendar/."""

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from skeleton.service_api import Forbidden

from .service_api import CalendarService


def home(request, organization_id):
    try:
        calendars = CalendarService().my_calendars(actor=request.user)
    except (PermissionDenied, Forbidden):
        raise Http404 from None
    return render(request, "skeleton_calendar/home.html", {"organization_id": organization_id, "calendars": calendars})


@require_POST
@transaction.atomic
def member_access(request, organization_id):
    from .services import grant_member_access

    try:
        granted = grant_member_access(actor=request.user)
    except Forbidden:
        raise Http404 from None
    messages.success(
        request, "Acesso básico concedido ao papel member." if granted else "O papel member já tinha acesso."
    )
    return redirect("skeleton_calendar:home", organization_id=organization_id)
