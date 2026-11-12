from django.urls import path

from skeleton.api.health import probe_response
from skeleton.web import admission_views, views
from skeleton.web.invitation_views import resend
from skeleton.web.organization_settings_views import organization_logo, organization_logo_update, organization_settings

app_name = "web"
urlpatterns = [
    path("", views.home, name="home"),
    path("profile/", views.profile, name="profile"),
    path("health/live/", probe_response, name="health-live"),
    path("health/ready/", probe_response, name="health-ready"),
    path("join/", admission_views.join_directory, name="join"),
    path("join/requests/<uuid:sysid>/withdraw/", admission_views.join_withdraw, name="join-withdraw"),
    path("join/<uuid:organization_id>/", admission_views.join_organization, name="join-organization"),
    path("join/domain/<uuid:organization_id>/", admission_views.join_by_domain, name="join-by-domain"),
    path("join/<uuid:organization_id>/<str:token>/", admission_views.join_link, name="join-link"),
    path("o/<uuid:organization_id>/", views.dashboard, name="dashboard"),
    path("o/<uuid:organization_id>/admission/", admission_views.admission_policy, name="admission"),
    path("o/<uuid:organization_id>/admission/links/", admission_views.join_links, name="join-links"),
    path("o/<uuid:organization_id>/admission/domains/", admission_views.verified_domains, name="verified-domains"),
    path(
        "o/<uuid:organization_id>/admission/domains/<uuid:sysid>/",
        admission_views.verified_domain_action,
        name="verified-domain-action",
    ),
    path(
        "o/<uuid:organization_id>/admission/links/<uuid:sysid>/revoke/",
        admission_views.join_link_revoke,
        name="join-link-revoke",
    ),
    path("o/<uuid:organization_id>/requests/", admission_views.join_requests, name="join-requests"),
    path(
        "o/<uuid:organization_id>/requests/<uuid:sysid>/decide/",
        admission_views.join_request_decide,
        name="join-request-decide",
    ),
    path("o/<uuid:organization_id>/leave/", views.leave_organization, name="leave-organization"),
    path("o/<uuid:organization_id>/settings/", organization_settings, name="organization-settings"),
    path("o/<uuid:organization_id>/settings/logo/", organization_logo, name="organization-logo"),
    path("o/<uuid:organization_id>/settings/logo/update/", organization_logo_update, name="organization-logo-update"),
    path("o/<uuid:organization_id>/catalog/", views.catalog, name="catalog"),
    path("o/<uuid:organization_id>/audit/", views.audit, name="audit"),
    path("o/<uuid:organization_id>/invitations/<uuid:sysid>/resend/", resend, name="invitation-resend"),
    path("o/<uuid:organization_id>/access/<str:resource>/", views.access_list, name="access-list"),
    path("o/<uuid:organization_id>/access/<str:resource>/new/", views.access_edit, name="access-create"),
    path("o/<uuid:organization_id>/access/<str:resource>/<uuid:sysid>/edit/", views.access_edit, name="access-edit"),
    path(
        "o/<uuid:organization_id>/access/<str:resource>/<uuid:sysid>/delete/", views.access_delete, name="access-delete"
    ),
    path(
        "o/<uuid:organization_id>/tables/<str:table_key>/references/<str:field_name>/",
        views.reference_picker,
        name="reference-picker",
    ),
    path("o/<uuid:organization_id>/tables/<str:table_key>/", views.records, name="records"),
    path("o/<uuid:organization_id>/tables/<str:table_key>/new/", views.record_edit, name="record-create"),
    path(
        "o/<uuid:organization_id>/tables/<str:table_key>/deleted/",
        views.records,
        {"deleted": True},
        name="deleted-records",
    ),
    path("o/<uuid:organization_id>/tables/<str:table_key>/<uuid:sysid>/", views.record_detail, name="record"),
    path("o/<uuid:organization_id>/tables/<str:table_key>/<uuid:sysid>/edit/", views.record_edit, name="record-edit"),
    path(
        "o/<uuid:organization_id>/tables/<str:table_key>/<uuid:sysid>/delete/",
        views.record_edit,
        {"operation": "delete"},
        name="record-delete",
    ),
    path(
        "o/<uuid:organization_id>/tables/<str:table_key>/<uuid:sysid>/restore/",
        views.record_edit,
        {"operation": "restore"},
        name="record-restore",
    ),
    path(
        "o/<uuid:organization_id>/tables/<str:table_key>/<uuid:sysid>/access/",
        views.record_edit,
        {"operation": "access"},
        name="record-access",
    ),
]
