from django.urls import path

from . import webhooks

app_name = "billing_webhooks"

urlpatterns = [
    path(
        "billing/webhooks/<slug:provider>/<uuid:account_id>/",
        webhooks.receive,
        name="receive",
    ),
]
