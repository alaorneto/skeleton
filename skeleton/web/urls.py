from django.urls import path

from skeleton.api.health import probe_response
from skeleton.web import views
from skeleton.web.invitation_views import resend

app_name = "web"
urlpatterns = [
    path("", views.home, name="home"),
    path("profile/", views.profile, name="profile"),
    path("health/live/", probe_response, name="health-live"),
    path("health/ready/", probe_response, name="health-ready"),
    path("catalog/", views.platform_catalog, name="platform-catalog"),
    path("audit/", views.platform_audit, name="platform-audit"),
    path("o/<uuid:organization_id>/", views.dashboard, name="dashboard"),
    path("o/<uuid:organization_id>/catalog/", views.catalog, name="catalog"),
    path("o/<uuid:organization_id>/audit/", views.audit, name="audit"),
    path("o/<uuid:organization_id>/invitations/<uuid:sysid>/resend/", resend, name="invitation-resend"),
    path("o/<uuid:organization_id>/access/<str:resource>/", views.access_list, name="access-list"),
    path("o/<uuid:organization_id>/access/<str:resource>/new/", views.access_edit, name="access-create"),
    path("o/<uuid:organization_id>/access/<str:resource>/<uuid:sysid>/edit/", views.access_edit, name="access-edit"),
    path("o/<uuid:organization_id>/access/<str:resource>/<uuid:sysid>/delete/", views.access_delete, name="access-delete"),
    path("o/<uuid:organization_id>/tables/<str:table_key>/references/<str:field_name>/", views.reference_picker, name="reference-picker"),
    path("o/<uuid:organization_id>/tables/<str:table_key>/", views.records, name="records"),
    path("o/<uuid:organization_id>/tables/<str:table_key>/new/", views.record_edit, name="record-create"),
    path("o/<uuid:organization_id>/tables/<str:table_key>/deleted/", views.records, {"deleted": True}, name="deleted-records"),
    path("o/<uuid:organization_id>/tables/<str:table_key>/<uuid:sysid>/", views.record_detail, name="record"),
    path("o/<uuid:organization_id>/tables/<str:table_key>/<uuid:sysid>/edit/", views.record_edit, name="record-edit"),
    path("o/<uuid:organization_id>/tables/<str:table_key>/<uuid:sysid>/delete/", views.record_edit, {"operation": "delete"}, name="record-delete"),
    path("o/<uuid:organization_id>/tables/<str:table_key>/<uuid:sysid>/restore/", views.record_edit, {"operation": "restore"}, name="record-restore"),
    path("o/<uuid:organization_id>/tables/<str:table_key>/<uuid:sysid>/access/", views.record_edit, {"operation": "access"}, name="record-access"),
]
