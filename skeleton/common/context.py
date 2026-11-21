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
    # An external person in the organization's portal (#531): no member power applies.
    external: bool = False


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


# The core's app labels all start with ``sk_``; the manifest validator refuses the prefix to apps.
CORE_LABEL_PREFIX = "sk_"


def is_app_global_table(model: Any) -> bool:
    """Whether ``model`` is a global table declared by an app (#576): no organization, label outside the core.

    Its writes require a system context without organization (``SystemRecord._validate_context``)
    and, once its infrastructure is frozen with ``writes: "system"``, the database guard too. The
    core's own global tables are written only by its services and stay outside this rule. Works
    on historical migration models too.
    """
    return not is_tenant_model(model) and not model._meta.app_label.startswith(CORE_LABEL_PREFIX)
