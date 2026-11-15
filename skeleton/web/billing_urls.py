from django.urls import path

from skeleton.tenancy.routing import ORG
from skeleton.web.billing import billing_action, billing_overview, billing_pay

urlpatterns = [
    path(f"{ORG}billing/", billing_overview, name="billing-overview"),
    path(f"{ORG}billing/<slug:action>/", billing_action, name="billing-action"),
    path(f"{ORG}billing/invoices/<uuid:invoice_id>/pay/", billing_pay, name="billing-pay"),
]
