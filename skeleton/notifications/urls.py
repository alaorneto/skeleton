from django.urls import path

from . import views

app_name = "notifications"
urlpatterns = [
    path("notifications/", views.global_inbox, name="global"),
    path("o/<uuid:organization_id>/notifications/", views.organization_inbox, name="organization"),
    path("o/<uuid:organization_id>/notifications/read-all/", views.read_all, name="read-all"),
    path("o/<uuid:organization_id>/notifications/<uuid:sysid>/open/", views.open_notification, name="open"),
    path(
        "o/<uuid:organization_id>/notifications/<uuid:sysid>/<str:action>/",
        views.change,
        name="change",
    ),
    path("api/v1/notifications/", views.InboxAPI.as_view(), name="api-inbox"),
    path("api/v1/o/<uuid:organization_id>/notifications/actions/", views.InboxActionAPI.as_view(), name="api-actions"),
]
