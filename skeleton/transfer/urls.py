from django.urls import path

from . import api_views, views

app_name = "transfer"
urlpatterns = [
    path("o/<uuid:organization_id>/transfers/", views.transfers, name="transfers"),
    path("o/<uuid:organization_id>/transfers/<uuid:sysid>/download/", views.download, name="download"),
    path("o/<uuid:organization_id>/tables/<str:table_key>/export/", views.request_export, name="request-export"),
    path("api/v1/o/<uuid:organization_id>/exports/", api_views.ExportsView.as_view(), name="api-exports"),
    path("api/v1/o/<uuid:organization_id>/transfers/", api_views.TransfersView.as_view(), name="api-transfers"),
    path(
        "api/v1/o/<uuid:organization_id>/transfers/<uuid:sysid>/",
        api_views.TransfersView.as_view(),
        name="api-transfer",
    ),
]
