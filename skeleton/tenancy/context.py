"""Explicit synchronous transaction contexts; never derive identity from request input."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any, Protocol
from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.db import connections, transaction

from skeleton.accounts.platform import is_platform_admin
from skeleton.common.exceptions import MissingTenantContext


class InvalidContext(RuntimeError):
    pass


class HasSysid(Protocol):
    @property
    def sysid(self) -> UUID: ...


# An identity may be a record, a UUID or its string form; the record's sysid wins.
IdentityRef = HasSysid | UUID | str | None


@dataclass(frozen=True)
class Context:
    actor: UUID | None = None
    organization_id: UUID | None = None
    request_id: UUID | None = None
    source: str = "sql:unattributed"
    platform_admin: bool = False
    system: bool = False
    using: str = "default"


# The only system contexts an HTTP request may open (docs/access-control.md). Each is chosen by
# server code after its own validation, never by a body or header value.
VALIDATED_IDENTITY_REASONS = frozenset({"invitation:validated", "sso:validated"})
PLATFORM_ADMIN_REASONS = frozenset(
    {
        "organization:create",
        "organization:bootstrap",
        "builtin-roles",
        "organization:designate-admin",
        "platform:organization-inspect",
    }
)
HTTP_SYSTEM_REASONS = VALIDATED_IDENTITY_REASONS | PLATFORM_ADMIN_REASONS

_context: ContextVar[Context | None] = ContextVar("skeleton_context", default=None)
_selected: ContextVar[UUID | None] = ContextVar("skeleton_selected_tenant", default=None)
_GUCS = ("user_id", "tenant_id", "request_id", "source", "platform_admin")


def current_context() -> Context:
    return _context.get() or Context()


def _uuid(value: IdentityRef) -> UUID | None:
    if value is None:
        return None
    return UUID(str(getattr(value, "sysid", value)))


def _apply(context: Context) -> None:
    connection = connections[context.using]
    if connection.vendor == "postgresql":
        values = (
            context.actor,
            context.organization_id,
            context.request_id,
            context.source,
            "on" if context.platform_admin else "off",
        )
        with connection.cursor() as cursor:
            for name, value in zip(_GUCS, values, strict=True):
                cursor.execute(
                    "SELECT set_config(%s, %s, true)", ["skeleton." + name, str(value) if value is not None else ""]
                )


@contextmanager
def _enter(context: Context) -> Iterator[Context]:
    prior = _context.get()
    if prior and prior.using != context.using:
        raise InvalidContext("A context cannot change database aliases.")
    with transaction.atomic(using=context.using):
        token = _context.set(context)
        try:
            _apply(context)
            yield context
        finally:
            _context.reset(token)
            connection = connections[context.using]
            if prior and not connection.needs_rollback:
                try:
                    _apply(prior)
                except Exception:
                    connection.close()
                    raise


@contextmanager
def request_context(
    *, actor: IdentityRef, request_id: IdentityRef, source: str, using: str = "default"
) -> Iterator[Context]:
    if _context.get() is not None:
        raise InvalidContext("Requests cannot nest.")
    selected_token = _selected.set(None)
    actor_id = _uuid(actor)
    context = Context(
        actor=actor_id,
        request_id=_uuid(request_id),
        source=source,
        platform_admin=is_platform_admin(actor),
        using=using,
    )
    try:
        with _enter(context) as active:
            yield active
    finally:
        _selected.reset(selected_token)


def _validate_membership(organization_id: UUID, actor_id: UUID | None, using: str) -> None:
    from skeleton.tenancy.models import Membership

    if (
        actor_id is None
        or not Membership.all_objects.using(using)
        .filter(
            organization_id=organization_id,
            user_id=actor_id,
            is_active=True,
            deleted_at__isnull=True,
            organization__is_active=True,
            organization__deleted_at__isnull=True,
            user__is_active=True,
            user__deleted_at__isnull=True,
        )
        .exists()
    ):
        raise PermissionDenied("Organization is unavailable.")


@contextmanager
def tenant_context(
    *, organization_id: IdentityRef, actor: IdentityRef, source: str, using: str = "default"
) -> Iterator[Context]:
    tenant_id, actor_id = _uuid(organization_id), _uuid(actor)
    prior = _context.get()
    if prior is None:
        # Install the actor GUC before self-discovery on PostgreSQL.
        with request_context(actor=actor, request_id=None, source=source, using=using):
            with tenant_context(organization_id=tenant_id, actor=actor, source=source, using=using) as active:
                yield active
        return
    if tenant_id is None:
        raise MissingTenantContext("Organization is required.")
    if prior and prior.actor != actor_id:
        raise InvalidContext("Actor may change only through the authentication hook.")
    if prior and prior.organization_id not in (None, tenant_id):
        raise InvalidContext("A transaction cannot cross tenants.")
    selected = _selected.get()
    if prior and selected not in (None, tenant_id):
        raise InvalidContext("A request may select only one tenant.")
    if not (prior and prior.system):
        # Membership has a deliberately restricted self-discovery SELECT policy.
        _validate_membership(tenant_id, actor_id, using)
    if prior:
        _selected.set(tenant_id)
    context = replace(prior or Context(using=using), actor=actor_id, organization_id=tenant_id, source=source)
    with _enter(context) as active:
        yield active


@contextmanager
def system_context(*, reason: str, organization_id: IdentityRef = None, using: str = "default") -> Iterator[Context]:
    if not isinstance(reason, str) or not reason.strip():
        raise InvalidContext("A system context requires a reason.")
    tenant_id = _uuid(organization_id)
    prior = _context.get()
    # These are private, narrow identity admission paths, never request-selectable flags.
    admitted = reason in VALIDATED_IDENTITY_REASONS or bool(
        prior and prior.platform_admin and reason in PLATFORM_ADMIN_REASONS
    )
    if prior and not prior.system and not admitted:
        raise InvalidContext("A request cannot promote itself to a system context.")
    if prior and prior.organization_id not in (None, tenant_id):
        raise InvalidContext("System work must use one transaction per tenant.")
    if prior and tenant_id is not None:
        if _selected.get() not in (None, tenant_id):
            raise InvalidContext("A request may select only one tenant.")
        _selected.set(tenant_id)
    context = replace(
        prior or Context(using=using), organization_id=tenant_id, source="system:" + reason.strip(), system=True
    )
    with _enter(context) as active:
        yield active


def refresh_authenticated_actor(user: Any) -> None:
    """Called only by completed login/logout signals, not intermediate MFA stages."""
    prior = _context.get()
    if prior is None:
        return
    if prior.organization_id is not None:
        raise InvalidContext("Authentication cannot change within a tenant transaction.")
    context = replace(
        prior,
        actor=_uuid(user) if getattr(user, "is_authenticated", False) else None,
        platform_admin=is_platform_admin(user),
    )
    _context.set(context)
    _apply(context)


def own_rows(model: Any, user: Any, *, using: str = "default") -> Any:
    """Live rows of ``model`` that belong to ``user`` in every organization (self-discovery).

    Only models that declare ``self_discovery_field`` (frozen as ``self_discovery_column``
    in their migration) qualify. The filter by user is explicit in Python, as SQLite has no
    RLS; on PostgreSQL the policy additionally limits cross-tenant reads to the actor GUC.
    The normal manager stays restricted to the current tenant, and writes never cross it.
    """
    field = getattr(model, "self_discovery_field", None)
    if not field:
        raise InvalidContext(f"{model._meta.label} does not declare self-discovery.")
    if not getattr(user, "is_authenticated", False):
        return model.all_objects.using(using).none()
    actor = current_context().actor
    if actor is not None and actor != user.pk:
        raise InvalidContext("Self-discovery reads only the current actor's rows.")
    column = model._meta.get_field(field).attname
    rows = model.all_objects.using(using).filter(**{column: user.pk})
    if any(item.name == "deleted_at" for item in model._meta.fields):
        rows = rows.filter(deleted_at__isnull=True)  # Operational tables have no tombstones.
    return rows


def memberships_for_user(user: Any, *, using: str = "default") -> list[dict[str, Any]]:
    from skeleton.tenancy.models import Membership

    if not getattr(user, "is_authenticated", False):
        return []
    from django.db.models import F

    return list(
        own_rows(Membership, user, using=using)
        .filter(
            is_active=True,
            organization__is_active=True,
            organization__deleted_at__isnull=True,
        )
        .values(
            "sysid",
            "organization_id",
            "is_active",
            organization_name=F("organization__name"),
            organization_slug=F("organization__slug"),
        )
    )
