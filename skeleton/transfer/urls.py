from django.urls import path

from skeleton.tenancy.routing import ORG

from . import views

app_name = "transfer"
urlpatterns = [
    path(f"{ORG}transfers/", views.transfer_list, name="list"),
    path(f"{ORG}transfers/<uuid:sysid>/download/", views.transfer_download, name="download"),
    path(f"{ORG}transfers/<uuid:sysid>/report/", views.report_download, name="report"),
    path(f"{ORG}transfers/<uuid:sysid>/apply/", views.import_confirm, name="apply"),
    path(f"{ORG}tables/<str:table_key>/export/", views.export_request, name="export"),
    path(f"{ORG}audit/export/", views.audit_export_request, name="audit-export"),
    path(f"{ORG}tables/<str:table_key>/import/", views.import_request, name="import"),
]
