from django.urls import path

from skeleton.web.billing import billing_action, billing_overview, billing_pay

urlpatterns = [
    path("o/<uuid:organization_id>/billing/", billing_overview, name="billing-overview"),
    path("o/<uuid:organization_id>/billing/<slug:action>/", billing_action, name="billing-action"),
    path("o/<uuid:organization_id>/billing/invoices/<uuid:invoice_id>/pay/", billing_pay, name="billing-pay"),
]
