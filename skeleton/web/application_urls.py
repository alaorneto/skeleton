"""Routes of the organization's apps panel (paths and names unchanged, #338)."""

from django.urls import path

from skeleton.web.applications import (
    application_admin,
    application_admin_screen,
    application_configuration,
    application_list,
)

urlpatterns = [
    path("o/<uuid:organization_id>/apps/", application_list, name="application-list"),
    path("o/<uuid:organization_id>/apps/<str:app_label>/admin/", application_admin, name="application-admin"),
    path(
        "o/<uuid:organization_id>/apps/<str:app_label>/admin/screens/<str:screen_id>/",
        application_admin_screen,
        name="application-admin-screen",
    ),
    path(
        "o/<uuid:organization_id>/apps/<str:app_label>/configuration/",
        application_configuration,
        name="application-configuration",
    ),
]
