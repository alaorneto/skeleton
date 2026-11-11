"""Domain events of the core lifecycle services (#43): memberships and app activation.

Each service publishes exactly one event per effective change, in its own transaction, so a
rollback leaves no event and a change without effect publishes nothing:

- ``membership.created``: a membership became active (new, restored or reactivated).
- ``membership.deactivated``: an active membership was deactivated, removed or left.
- ``app.enabled`` / ``app.disabled``: an organization turned an app on or off.

The payload carries identifiers only (``user`` of the membership, ``app_label`` of the app);
consumers re-read and re-authorize whatever else they need. These types are not offered to
webhooks: the delivery re-authorizes a record read by the endpoint principal, and these
tables have no record-level authorization.
"""

from __future__ import annotations

from typing import Any

MEMBERSHIP_CREATED = "membership.created"
MEMBERSHIP_DEACTIVATED = "membership.deactivated"
APP_ENABLED = "app.enabled"
APP_DISABLED = "app.disabled"
CORE_EVENT_TYPES = (MEMBERSHIP_CREATED, MEMBERSHIP_DEACTIVATED, APP_ENABLED, APP_DISABLED)
MEMBERSHIP_TABLE = "sk_tenancy.membership"
APP_TABLE = "sk_system.appinstallation"


def register_core_event_types() -> None:
    from .registry import EventType, register_event_type

    for code in (MEMBERSHIP_CREATED, MEMBERSHIP_DEACTIVATED):
        register_event_type(
            EventType(code, table_key=MEMBERSHIP_TABLE, payload_fields=("user",), owner_app="sk_tenancy")
        )
    for code in (APP_ENABLED, APP_DISABLED):
        register_event_type(EventType(code, table_key=APP_TABLE, payload_fields=("app_label",), owner_app="sk_system"))


def membership_changed(membership: Any, *, was_active: bool) -> None:
    """Publish the transition of ``membership`` from ``was_active`` to its current state, if any."""
    from .services import publish_event

    active = bool(membership.is_active) and membership.deleted_at is None
    if active == was_active:
        return
    code = MEMBERSHIP_CREATED if active else MEMBERSHIP_DEACTIVATED
    publish_event(code, record=membership, data={"user": membership.user_id})


def application_changed(installation: Any, *, was_enabled: bool) -> None:
    from .services import publish_event

    if bool(installation.enabled) == was_enabled:
        return
    code = APP_ENABLED if installation.enabled else APP_DISABLED
    publish_event(code, record=installation, data={"app_label": installation.app_label})
