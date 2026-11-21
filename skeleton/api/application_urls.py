"""Routes of the REST endpoint of the organization's apps (paths and names unchanged, #338)."""

from django.urls import path

from skeleton.api.applications import ApplicationAPI
from skeleton.api.discovery import ActiveApplicationsView

urlpatterns = [
    path("api/v1/o/<uuid:organization_id>/apps/", ApplicationAPI.as_view(), name="api-application-list"),
    # Any active member, session or token (#605); before the app routes under apps/<label>/.
    path(
        "api/v1/o/<uuid:organization_id>/apps/active/",
        ActiveApplicationsView.as_view(),
        name="api-application-active",
    ),
    path(
        "api/v1/o/<uuid:organization_id>/apps/<str:app_label>/configuration/",
        ApplicationAPI.as_view(),
        name="api-application-configuration",
    ),
]
