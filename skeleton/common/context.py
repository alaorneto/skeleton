"""The current execution context, as plain state (#307).

``skeleton/common`` reads it without depending on ``skeleton/tenancy``: the context managers that
open transactions, set the PostgreSQL GUCs and validate memberships stay in
``skeleton.tenancy.context``, which re-exports these names (the public surface for apps).
"""

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class Context:
    actor: UUID | None = None
    organization_id: UUID | None = None
    request_id: UUID | None = None
    source: str = "sql:unattributed"
    platform_admin: bool = False
    system: bool = False
    using: str = "default"
    # Scopes of the API token that authenticated the request (#46); None for a session.
    credential_scopes: frozenset[str] | None = None
    # Global audit reading by the platform auditor (#34); only inside ``platform_audit_scope``.
    platform_auditor: bool = False


_context: ContextVar[Context | None] = ContextVar("skeleton_context", default=None)


def current_context() -> Context:
    return _context.get() or Context()


def in_context() -> bool:
    """Whether any request, tenant or system context is open in this execution context."""
    return _context.get() is not None


def is_tenant_model(model: Any) -> bool:
    """Whether ``model`` belongs to an organization: it declares the ``organization`` field.

    The field name is the contract (check skeleton.E017 forbids it on global records): it
    turns on tenant filtering in managers, tenant validation on writes and tenant RLS in DDL.
    Works on historical migration models too.
    """
    return any(field.name == "organization" for field in model._meta.fields)
