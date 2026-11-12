from django.urls import path

from . import views

app_name = "transfer"
urlpatterns = [
    path("o/<uuid:organization_id>/transfers/", views.transfer_list, name="list"),
    path("o/<uuid:organization_id>/transfers/<uuid:sysid>/download/", views.transfer_download, name="download"),
    path("o/<uuid:organization_id>/tables/<str:table_key>/export/", views.export_request, name="export"),
]
