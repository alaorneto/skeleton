from django.urls import path

from . import updates, views

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
    path("updates/", updates.installed, name="updates"),
    path("updates/available/", updates.available, name="updates-available"),
    path("updates/fetch/", updates.fetch_release, name="updates-fetch"),
    path("updates/upload/", updates.upload, name="updates-upload"),
    path("updates/plan/", updates.plan, name="updates-plan"),
    path("updates/plan/add-dependency/", updates.add_dependency, name="updates-add-dependency"),
    path("updates/operations/", updates.operations, name="updates-operations"),
    path("updates/operations/<uuid:operation_id>/", updates.operation, name="updates-operation"),
]
