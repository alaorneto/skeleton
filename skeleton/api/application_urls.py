"""Routes of the REST endpoint of the organization's apps (paths and names unchanged, #338)."""

from django.urls import path

from skeleton.api.applications import ApplicationAPI

urlpatterns = [
    path("api/v1/o/<uuid:organization_id>/apps/", ApplicationAPI.as_view(), name="api-application-list"),
    path(
        "api/v1/o/<uuid:organization_id>/apps/<str:app_label>/configuration/",
        ApplicationAPI.as_view(),
        name="api-application-configuration",
    ),
]
