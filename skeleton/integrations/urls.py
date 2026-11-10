from django.urls import path

from .webhooks import api_views, views

app_name = "integrations"
urlpatterns = [
    path("o/<uuid:organization_id>/integrations/webhooks/", views.endpoint_list, name="webhooks"),
    path("o/<uuid:organization_id>/integrations/webhooks/new/", views.endpoint_create, name="webhook-create"),
    path("o/<uuid:organization_id>/integrations/webhooks/<uuid:sysid>/", views.endpoint_detail, name="webhook"),
    path(
        "o/<uuid:organization_id>/integrations/webhooks/<uuid:sysid>/rotate/",
        views.endpoint_rotate,
        name="webhook-rotate",
    ),
    path(
        "o/<uuid:organization_id>/integrations/webhooks/<uuid:sysid>/delete/",
        views.endpoint_delete,
        name="webhook-delete",
    ),
    path(
        "o/<uuid:organization_id>/integrations/webhooks/<uuid:sysid>/deliveries/",
        views.delivery_list,
        name="webhook-deliveries",
    ),
    path(
        "o/<uuid:organization_id>/integrations/webhooks/<uuid:sysid>/deliveries/<uuid:delivery_id>/resend/",
        views.delivery_resend,
        name="webhook-delivery-resend",
    ),
    path("o/<uuid:organization_id>/integrations/webhooks/<uuid:sysid>/ping/", views.endpoint_ping, name="webhook-ping"),
    path("api/v1/o/<uuid:organization_id>/webhooks/", api_views.WebhooksView.as_view(), name="api-webhooks"),
    path(
        "api/v1/o/<uuid:organization_id>/webhooks/<uuid:sysid>/deliveries/",
        api_views.DeliveriesView.as_view(),
        name="api-webhook-deliveries",
    ),
    path(
        "api/v1/o/<uuid:organization_id>/webhooks/<uuid:sysid>/deliveries/<uuid:delivery_id>/",
        api_views.DeliveryDetailView.as_view(),
        name="api-webhook-delivery",
    ),
    path(
        "api/v1/o/<uuid:organization_id>/webhooks/<uuid:sysid>/deliveries/<uuid:delivery_id>/resend/",
        api_views.DeliveryResendView.as_view(),
        name="api-webhook-delivery-resend",
    ),
    path(
        "api/v1/o/<uuid:organization_id>/webhooks/<uuid:sysid>/ping/",
        api_views.WebhookPingView.as_view(),
        name="api-webhook-ping",
    ),
    path(
        "api/v1/o/<uuid:organization_id>/webhooks/<uuid:sysid>/",
        api_views.WebhookDetailView.as_view(),
        name="api-webhook",
    ),
    path(
        "api/v1/o/<uuid:organization_id>/webhooks/<uuid:sysid>/rotate/",
        api_views.WebhookRotateView.as_view(),
        name="api-webhook-rotate",
    ),
]
