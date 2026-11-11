from django.urls import path

from .tokens import api_views as token_api
from .tokens import views as tokens
from .webhooks import api_views, views

app_name = "integrations"
urlpatterns = [
    path("profile/tokens/", tokens.personal_tokens, name="personal-tokens"),
    path("profile/tokens/<uuid:sysid>/revoke/", tokens.personal_token_revoke, name="personal-token-revoke"),
    path("o/<uuid:organization_id>/integrations/service-accounts/", tokens.service_accounts, name="service-accounts"),
    path(
        "o/<uuid:organization_id>/integrations/service-accounts/<uuid:sysid>/",
        tokens.service_account,
        name="service-account",
    ),
    path(
        "o/<uuid:organization_id>/integrations/service-accounts/<uuid:sysid>/deactivate/",
        tokens.service_account_deactivate,
        name="service-account-deactivate",
    ),
    path(
        "o/<uuid:organization_id>/integrations/service-accounts/<uuid:sysid>/tokens/<uuid:token_id>/revoke/",
        tokens.service_token_revoke,
        name="service-token-revoke",
    ),
    path("api/v1/me/tokens/", token_api.MeTokensView.as_view(), name="api-me-tokens"),
    path("api/v1/me/tokens/<uuid:sysid>/", token_api.MeTokenDetailView.as_view(), name="api-me-token"),
    path(
        "api/v1/o/<uuid:organization_id>/service-accounts/",
        token_api.ServiceAccountsView.as_view(),
        name="api-service-accounts",
    ),
    path(
        "api/v1/o/<uuid:organization_id>/service-accounts/<uuid:sysid>/",
        token_api.ServiceAccountDetailView.as_view(),
        name="api-service-account",
    ),
    path(
        "api/v1/o/<uuid:organization_id>/service-accounts/<uuid:sysid>/tokens/",
        token_api.ServiceAccountTokensView.as_view(),
        name="api-service-account-tokens",
    ),
    path(
        "api/v1/o/<uuid:organization_id>/service-accounts/<uuid:sysid>/tokens/<uuid:token_id>/",
        token_api.ServiceAccountTokenDetailView.as_view(),
        name="api-service-account-token",
    ),
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
