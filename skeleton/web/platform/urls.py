from django.urls import path

from . import views

app_name = "platform"
urlpatterns = [
    path("", views.overview, name="overview"),
    path("users/", views.users, name="users"),
    path("users/new/", views.user_create, name="user-create"),
    path("users/<uuid:sysid>/", views.user_detail, name="user"),
    path("users/<uuid:sysid>/delete/", views.user_delete, name="user-delete"),
    path("organizations/", views.organizations, name="organizations"),
    path("organizations/new/", views.organization_create, name="organization-create"),
    path("organizations/<uuid:sysid>/", views.organization_detail, name="organization"),
    path("catalog/", views.catalog, name="catalog"),
    path("catalog/<uuid:sysid>/", views.catalog_table, name="catalog-table"),
    path("catalog/fields/<uuid:sysid>/", views.catalog_field, name="catalog-field"),
    path("audit/", views.audit, name="audit"),
    path("apps/", views.app_list, name="apps"),
    path("apps/<str:app_label>/", views.app_detail, name="app"),
    path("apps/<str:app_label>/documentation/", views.app_documentation, name="app-documentation"),
    path("apps/<str:app_label>/screens/<str:screen_id>/", views.app_screen, name="app-screen"),
]
