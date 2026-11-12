from django.urls import path

from . import views

app_name = "transfer"
urlpatterns = [
    path("o/<uuid:organization_id>/transfers/", views.transfer_list, name="list"),
    path("o/<uuid:organization_id>/transfers/<uuid:sysid>/download/", views.transfer_download, name="download"),
    path("o/<uuid:organization_id>/transfers/<uuid:sysid>/report/", views.report_download, name="report"),
    path("o/<uuid:organization_id>/transfers/<uuid:sysid>/apply/", views.import_confirm, name="apply"),
    path("o/<uuid:organization_id>/tables/<str:table_key>/export/", views.export_request, name="export"),
    path("o/<uuid:organization_id>/audit/export/", views.audit_export_request, name="audit-export"),
    path("o/<uuid:organization_id>/tables/<str:table_key>/import/", views.import_request, name="import"),
]
