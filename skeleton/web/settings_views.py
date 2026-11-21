"""The settings area of an organization (#621): the hub ``/o/<org>/admin/``.

Only presentation: the hub lists, by group, the entries of ``SETTINGS_GROUPS`` this person sees;
each target view keeps its own authorization. Without any entry besides the ones every member sees
(Tables), the hub does not exist for that person (the same neutral 404 as any missing page).
"""

from django.http import Http404
from django.shortcuts import render
from django.utils.translation import gettext

from skeleton.web.navigation import can_open_settings, settings_navigation_for
from skeleton.web.views import tenant_page

__all__ = ["organization_admin"]


@tenant_page()
def organization_admin(request, organization_id):
    groups = settings_navigation_for(request, organization_id)
    if not can_open_settings(groups):
        raise Http404
    return render(
        request,
        "skeleton/web/settings/hub.html",
        {"title": gettext("Configuração da organização"), "groups": groups, "organization_id": organization_id},
    )
