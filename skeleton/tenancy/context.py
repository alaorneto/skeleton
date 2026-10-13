"""Explicit synchronous transaction contexts; never derive identity from request input."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.db import connections, transaction
from skeleton.common.exceptions import MissingTenantContext


class InvalidContext(RuntimeError):
    pass


@dataclass(frozen=True)
class Context:
    actor: UUID | None = None
    organization_id: UUID | None = None
    request_id: UUID | None = None
    source: str = "sql:unattributed"
    platform_admin: bool = False
    system: bool = False
    using: str = "default"


_context = ContextVar("skeleton_context", default=None)
_selected = ContextVar("skeleton_selected_tenant", default=None)
_GUCS = ("user_id", "tenant_id", "request_id", "source", "platform_admin")


def current_context():
    return _context.get() or Context()


def _uuid(value):
    if value is None:
        return None
    return UUID(str(getattr(value, "sysid", value)))


def _apply(context):
    connection = connections[context.using]
    if connection.vendor == "postgresql":
        values = (context.actor, context.organization_id, context.request_id, context.source,
                  "on" if context.platform_admin else "off")
        with connection.cursor() as cursor:
            for name, value in zip(_GUCS, values):
                cursor.execute("SELECT set_config(%s, %s, true)", ["skeleton." + name, str(value) if value is not None else ""])


@contextmanager
def _enter(context):
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
def request_context(*, actor, request_id, source, using="default"):
    if _context.get() is not None:
        raise InvalidContext("Requests cannot nest.")
    selected_token = _selected.set(None)
    actor_id = _uuid(actor)
    context = Context(actor=actor_id, request_id=_uuid(request_id), source=source,
                      platform_admin=bool(getattr(actor, "is_superuser", False) and getattr(actor, "is_active", False)), using=using)
    try:
        with _enter(context) as active:
            yield active
    finally:
        _selected.reset(selected_token)


def _validate_membership(organization_id, actor_id, using):
    from skeleton.tenancy.models import Membership
    if actor_id is None or not Membership.all_objects.using(using).filter(
        organization_id=organization_id, user_id=actor_id, is_active=True, deleted_at__isnull=True,
        organization__is_active=True, organization__deleted_at__isnull=True,
        user__is_active=True, user__deleted_at__isnull=True,
    ).exists():
        raise PermissionDenied("Organization is unavailable.")


@contextmanager
def tenant_context(*, organization_id, actor, source, using="default"):
    organization_id, actor_id = _uuid(organization_id), _uuid(actor)
    prior = _context.get()
    if prior is None:
        # Install the actor GUC before self-discovery on PostgreSQL.
        with request_context(actor=actor, request_id=None, source=source, using=using):
            with tenant_context(organization_id=organization_id, actor=actor, source=source, using=using) as active:
                yield active
        return
    if organization_id is None:
        raise MissingTenantContext("Organization is required.")
    if prior and prior.actor != actor_id:
        raise InvalidContext("Actor may change only through the authentication hook.")
    if prior and prior.organization_id not in (None, organization_id):
        raise InvalidContext("A transaction cannot cross tenants.")
    selected = _selected.get()
    if prior and selected not in (None, organization_id):
        raise InvalidContext("A request may select only one tenant.")
    if not (prior and prior.system):
        # Membership has a deliberately restricted self-discovery SELECT policy.
        _validate_membership(organization_id, actor_id, using)
    if prior:
        _selected.set(organization_id)
    context = replace(prior or Context(using=using), actor=actor_id, organization_id=organization_id, source=source)
    with _enter(context) as active:
        yield active


@contextmanager
def system_context(*, reason, organization_id=None, using="default"):
    if not isinstance(reason, str) or not reason.strip():
        raise InvalidContext("A system context requires a reason.")
    organization_id = _uuid(organization_id)
    prior = _context.get()
    # These are private, narrow identity admission paths, never request-selectable flags.
    admitted = reason in {"invitation:validated", "sso:validated"} or bool(prior and prior.platform_admin and reason in {"organization:create", "organization:bootstrap", "builtin-permissions"})
    if prior and not prior.system and not admitted:
        raise InvalidContext("A request cannot promote itself to a system context.")
    if prior and prior.organization_id not in (None, organization_id):
        raise InvalidContext("System work must use one transaction per tenant.")
    if prior and organization_id is not None:
        if _selected.get() not in (None, organization_id):
            raise InvalidContext("A request may select only one tenant.")
        _selected.set(organization_id)
    context = replace(prior or Context(using=using), organization_id=organization_id,
                      source="system:" + reason.strip(), system=True)
    with _enter(context) as active:
        yield active


def refresh_authenticated_actor(user):
    """Called only by completed login/logout signals, not intermediate MFA stages."""
    prior = _context.get()
    if prior is None:
        return
    if prior.organization_id is not None:
        raise InvalidContext("Authentication cannot change within a tenant transaction.")
    context = replace(prior, actor=_uuid(user) if getattr(user, "is_authenticated", False) else None,
                      platform_admin=bool(getattr(user, "is_superuser", False) and getattr(user, "is_active", False)))
    _context.set(context)
    _apply(context)


def memberships_for_user(user, *, using="default"):
    from skeleton.tenancy.models import Membership
    if not getattr(user, "is_authenticated", False):
        return []
    from django.db.models import F
    return list(Membership.all_objects.using(using).filter(user_id=user.pk, is_active=True,
        deleted_at__isnull=True, organization__is_active=True, organization__deleted_at__isnull=True
    ).values("sysid", "organization_id", "is_active",
             organization_name=F("organization__name"), organization_slug=F("organization__slug")))
