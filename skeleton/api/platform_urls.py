"""Routes of the platform API (#709, #711): read-only, under ``/api/v1/platform/``."""

from django.urls import path

from skeleton.api import platform

urlpatterns = [
    path("platform/instance/", platform.InstanceView.as_view(), name="platform-instance"),
    path("platform/organizations/", platform.OrganizationsView.as_view(), name="platform-organizations"),
    path(
        "platform/organizations/<uuid:sysid>/",
        platform.OrganizationView.as_view(),
        name="platform-organization",
    ),
    path(
        "platform/organizations/<uuid:sysid>/invoices/",
        platform.OrganizationInvoicesView.as_view(),
        name="platform-organization-invoices",
    ),
    path(
        "platform/organizations/<uuid:sysid>/payments/",
        platform.OrganizationPaymentsView.as_view(),
        name="platform-organization-payments",
    ),
    path(
        "platform/organizations/<uuid:sysid>/billing-account/",
        platform.OrganizationBillingAccountView.as_view(),
        name="platform-organization-billing-account",
    ),
    path("platform/metrics/", platform.MetricsView.as_view(), name="platform-metrics"),
]
