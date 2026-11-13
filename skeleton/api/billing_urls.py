from django.urls import path

from skeleton.api.billing import BillingActionAPI, BillingAPI

urlpatterns = [
    path("api/v1/o/<uuid:organization_id>/billing/", BillingAPI.as_view(), name="api-billing"),
    path(
        "api/v1/o/<uuid:organization_id>/billing/<slug:action>/", BillingActionAPI.as_view(), name="api-billing-action"
    ),
]
