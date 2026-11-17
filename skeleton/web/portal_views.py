"""The core pages of the portal of external people (#533): the person's organizations and an organization's apps."""

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.urls import NoReverseMatch, reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from skeleton.tenancy.aggregate import aggregate_route, for_each_external_organization
from skeleton.web.views import portal_page


@aggregate_route
@never_cache
@login_required
@require_safe
def portal(request):
    """The organizations where the person has an active external access and the portal is on."""
    found = for_each_external_organization(request.user, lambda organization_id: True, request_id=request.request_id)
    organizations = [{"organization_id": item.organization_id, "name": item.organization_name} for item in found]
    return render(request, "skeleton/portal/home.html", {"organizations": organizations})


def _portal_apps(organization_id):
    from django.core.exceptions import PermissionDenied

    from skeleton.system.app_registry import all_apps
    from skeleton.system.applications import require_application_enabled

    from .app_routing import portal_namespace

    apps = []
    for manifest in all_apps():
        if not manifest.portal_urls:
            continue
        try:
            require_application_enabled(manifest.app_label)
        except PermissionDenied:
            continue
        try:
            url = reverse(f"{portal_namespace(manifest.app_label)}:home", kwargs={"organization_id": organization_id})
        except NoReverseMatch:
            url = f"/portal/{organization_id}/apps/{manifest.app_label}/"
        apps.append({"name": manifest.name, "url": url})
    return apps


@portal_page
@require_safe
def portal_organization(request, organization_id):
    """The apps with a portal that are active in the organization."""
    return render(request, "skeleton/portal/organization.html", {"apps": _portal_apps(organization_id)})
