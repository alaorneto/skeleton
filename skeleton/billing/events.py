"""Domain events of billing (#396).

- ``billing.invoice_finalized``: an invoice was issued (number, status, currency and total).
- ``billing.invoice_paid``: an invoice was paid. An external fiscal emitter (NFS-e) consumes it
  in process with ``register_consumer``.
- ``billing.subscription_changed``: the subscription started, changed plan, renewed out of a
  trial, was scheduled to cancel or was canceled (its status).
- ``billing.access_state_changed``: the derived access level of the organization changed (#399).

Identifiers and amounts only, never contact data of the account. These types are not offered
to outbound webhooks: the delivery re-authorizes a record read by the endpoint principal, and
billing tables have no record-level authorization.
"""

from __future__ import annotations

INVOICE_FIELDS = ("number", "status", "currency", "total")
EVENT_TYPES = (
    ("billing.invoice_finalized", "sk_billing.invoice", INVOICE_FIELDS),
    ("billing.invoice_paid", "sk_billing.invoice", INVOICE_FIELDS),
    ("billing.subscription_changed", "sk_billing.subscription", ("status",)),
    ("billing.access_state_changed", "sk_billing.billingaccount", ("access_state",)),
)


def register_billing_events() -> None:
    from skeleton.events.registry import EventType, register_event_type

    for code, table_key, fields in EVENT_TYPES:
        register_event_type(EventType(code, table_key=table_key, payload_fields=fields, owner_app="sk_billing"))
