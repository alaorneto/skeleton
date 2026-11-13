"""Billing reactions to the core lifecycle (#396)."""

from __future__ import annotations

from typing import Any


def organization_created(organization: Any) -> None:
    """Give a new organization its billing account and the default plan's trial, with billing on."""
    from . import engine

    if engine.billing_enabled():
        engine.start_default_subscription()
