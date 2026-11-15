"""Routes of the organization's apps panel (paths and names unchanged, #338)."""

from django.urls import path

from skeleton.tenancy.routing import ORG
from skeleton.web.applications import (
    application_admin,
    application_admin_screen,
    application_configuration,
    application_list,
)

urlpatterns = [
    path(f"{ORG}apps/", application_list, name="application-list"),
    path(f"{ORG}apps/<str:app_label>/admin/", application_admin, name="application-admin"),
    path(
        f"{ORG}apps/<str:app_label>/admin/screens/<str:screen_id>/",
        application_admin_screen,
        name="application-admin-screen",
    ),
    path(
        f"{ORG}apps/<str:app_label>/configuration/",
        application_configuration,
        name="application-configuration",
    ),
]
