"""Subscriptions, invoices and payments of the platform sold as SaaS (epic #406).

The core is the source of truth: the catalog, each organization's subscription and the
invoices live here; a payment provider only collects an invoice. Off by default
(``SKELETON_BILLING_ENABLED``); see docs/billing.md.
"""
