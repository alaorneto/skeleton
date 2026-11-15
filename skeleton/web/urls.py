from django.urls import path

from skeleton.api.health import probe_response
from skeleton.tenancy.routing import ORG
from skeleton.web import admission_views, identity_views, privacy_views, views
from skeleton.web.invitation_views import resend
from skeleton.web.organization_settings_views import (
    organization_logo,
    organization_logo_update,
    organization_settings,
    site_logo,
)

app_name = "web"
urlpatterns = [
    path("", views.home, name="home"),
    path("profile/", views.profile, name="profile"),
    path("profile/privacy/", privacy_views.privacy, name="privacy"),
    path("profile/privacy/export/", privacy_views.privacy_export, name="privacy-export"),
    path("profile/privacy/erasure/", privacy_views.privacy_erasure, name="privacy-erasure"),
    path("health/live/", probe_response, name="health-live"),
    path("health/ready/", probe_response, name="health-ready"),
    path("join/", admission_views.join_directory, name="join"),
    path("join/requests/<uuid:sysid>/withdraw/", admission_views.join_withdraw, name="join-withdraw"),
    # Public brand of the organization host (#484, #485): 404 on the main domain.
    path("site/logo/", site_logo, name="site-logo"),
    path("join/<uuid:organization_id>/", admission_views.join_organization, name="join-organization"),
    path("join/domain/<uuid:organization_id>/", admission_views.join_by_domain, name="join-by-domain"),
    path("join/<uuid:organization_id>/<str:token>/", admission_views.join_link, name="join-link"),
    path(ORG, views.dashboard, name="dashboard"),
    path(f"{ORG}admission/", admission_views.admission_policy, name="admission"),
    path(f"{ORG}identity/", identity_views.identity, name="identity"),
    path(
        f"{ORG}identity/<uuid:sysid>/",
        identity_views.identity_admission_update,
        name="identity-admission",
    ),
    path(f"{ORG}admission/links/", admission_views.join_links, name="join-links"),
    path(f"{ORG}admission/domains/", admission_views.verified_domains, name="verified-domains"),
    path(
        f"{ORG}admission/domains/<uuid:sysid>/",
        admission_views.verified_domain_action,
        name="verified-domain-action",
    ),
    path(
        f"{ORG}admission/links/<uuid:sysid>/revoke/",
        admission_views.join_link_revoke,
        name="join-link-revoke",
    ),
    path(f"{ORG}requests/", admission_views.join_requests, name="join-requests"),
    path(
        f"{ORG}requests/<uuid:sysid>/decide/",
        admission_views.join_request_decide,
        name="join-request-decide",
    ),
    path(f"{ORG}leave/", views.leave_organization, name="leave-organization"),
    path(f"{ORG}settings/", organization_settings, name="organization-settings"),
    path(f"{ORG}settings/logo/", organization_logo, name="organization-logo"),
    path(f"{ORG}settings/logo/update/", organization_logo_update, name="organization-logo-update"),
    path(f"{ORG}catalog/", views.catalog, name="catalog"),
    path(f"{ORG}audit/", views.audit, name="audit"),
    path(f"{ORG}invitations/<uuid:sysid>/resend/", resend, name="invitation-resend"),
    path(f"{ORG}access/<str:resource>/", views.access_list, name="access-list"),
    path(f"{ORG}access/<str:resource>/new/", views.access_edit, name="access-create"),
    path(f"{ORG}access/<str:resource>/<uuid:sysid>/edit/", views.access_edit, name="access-edit"),
    path(f"{ORG}access/<str:resource>/<uuid:sysid>/delete/", views.access_delete, name="access-delete"),
    path(
        f"{ORG}tables/<str:table_key>/references/<str:field_name>/",
        views.reference_picker,
        name="reference-picker",
    ),
    path(f"{ORG}tables/<str:table_key>/", views.records, name="records"),
    path(f"{ORG}tables/<str:table_key>/new/", views.record_edit, name="record-create"),
    path(
        f"{ORG}tables/<str:table_key>/deleted/",
        views.records,
        {"deleted": True},
        name="deleted-records",
    ),
    path(f"{ORG}tables/<str:table_key>/<uuid:sysid>/", views.record_detail, name="record"),
    path(f"{ORG}tables/<str:table_key>/<uuid:sysid>/edit/", views.record_edit, name="record-edit"),
    path(
        f"{ORG}tables/<str:table_key>/<uuid:sysid>/delete/",
        views.record_edit,
        {"operation": "delete"},
        name="record-delete",
    ),
    path(
        f"{ORG}tables/<str:table_key>/<uuid:sysid>/restore/",
        views.record_edit,
        {"operation": "restore"},
        name="record-restore",
    ),
    path(
        f"{ORG}tables/<str:table_key>/<uuid:sysid>/access/",
        views.record_edit,
        {"operation": "access"},
        name="record-access",
    ),
]
