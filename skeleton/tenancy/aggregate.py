"""Read authorized data of several organizations in one request (#74).

A request normally selects one tenant (``_selected`` in ``skeleton.tenancy.context``). Views
that show a person's items across organizations, such as "Minha agenda" or a calendar feed,
are marked with :func:`aggregate_route`: ``TenantContextMiddleware`` then opens no context and
no transaction for them, and the view calls :func:`for_each_organization`, which runs ``fn``
once per organization in a **full context of its own**:

- the person's active memberships are listed first, in one short transaction, and capped at
  ``limit`` (50) organizations, ordered by name;
- each organization runs in its own ``request_context`` + ``tenant_context`` transaction, so
  TablePermission, record visibility, app activation and RLS apply exactly as in a normal
  request; on PostgreSQL the transaction is ``READ ONLY``, so a write inside ``fn`` fails;
- an organization whose membership became inactive, where ``app_label`` is not active, or where
  ``fn`` is denied (``PermissionDenied``, or ``Forbidden``/``Unavailable`` of a service API), is
  skipped silently;
- ``fn`` must return materialized values (no queryset, iterator or generator), because nothing
  may be read after its transaction ends;
- ``credential_scopes`` carries the scopes of the API token of the request into every context,
  so a token stays narrowed in each organization (``None``, the default, is a session).

Writing across organizations is out of scope: a write always happens in one organization.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from types import GeneratorType
from typing import Any
from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.db import connections
from django.db.models.query import QuerySet

from skeleton.common.exceptions import ApplicationUnavailable
from skeleton.service_api.errors import Forbidden, Unavailable

from .context import InvalidContext, in_context, request_context, tenant_context

DEFAULT_LIMIT = 50


class LazyAggregateResult(TypeError):
    """``fn`` returned something that would read the database after its transaction."""


@dataclass(frozen=True, slots=True)
class OrganizationResult:
    organization_id: UUID
    organization_name: str
    value: Any


def aggregate_route(view: Callable) -> Callable:
    """Mark a view that reads many organizations: the tenant middleware leaves it without context."""
    view.skeleton_aggregate = True  # type: ignore[attr-defined]
    return view


def is_aggregate_route(callback: Callable | None) -> bool:
    return bool(callback is not None and getattr(callback, "skeleton_aggregate", False))


def _materialized(value: Any) -> Any:
    if isinstance(value, QuerySet | Iterator | GeneratorType):
        raise LazyAggregateResult("Return materialized values (list, tuple, dict, DTOs), never a lazy result.")
    return value


def _organizations(user: Any, *, request_id: Any, source: str, limit: int, using: str) -> list[tuple[UUID, str]]:
    from .models import Membership

    with request_context(actor=user, request_id=request_id, source=source, using=using):
        # The membership self-discovery policy shows the actor's own rows only.
        rows = (
            Membership.all_objects.using(using)
            .filter(
                user_id=user.pk,
                is_active=True,
                deleted_at__isnull=True,
                organization__is_active=True,
                organization__deleted_at__isnull=True,
            )
            .order_by("organization__name", "organization_id")
            .values_list("organization_id", "organization__name")[:limit]
        )
        return list(rows)


def for_each_organization(
    user: Any,
    fn: Callable[[Any], Any],
    *,
    source: str,
    request_id: Any = None,
    app_label: str | None = None,
    limit: int = DEFAULT_LIMIT,
    using: str = "default",
    credential_scopes: frozenset[str] | None = None,
) -> list[OrganizationResult]:
    """Run ``fn(organization_id)`` in each of the user's organizations; return the materialized results."""
    if in_context():
        raise InvalidContext("for_each_organization runs outside any context (mark the view with aggregate_route).")
    if not getattr(user, "is_authenticated", False):
        return []
    if not 1 <= limit <= DEFAULT_LIMIT:
        raise ValueError(f"limit must be between 1 and {DEFAULT_LIMIT}.")
    results = []
    for organization_id, name in _organizations(user, request_id=request_id, source=source, limit=limit, using=using):
        try:
            with request_context(
                actor=user, request_id=request_id, source=source, using=using, credential_scopes=credential_scopes
            ):
                with tenant_context(organization_id=organization_id, actor=user, source=source, using=using):
                    if connections[using].vendor == "postgresql":
                        with connections[using].cursor() as cursor:
                            cursor.execute("SET TRANSACTION READ ONLY")
                    if app_label is not None:
                        from skeleton.system.applications import require_application_enabled

                        require_application_enabled(app_label)
                    value = _materialized(fn(organization_id))
        except (PermissionDenied, ApplicationUnavailable, Forbidden, Unavailable):
            continue  # Membership inactive since the listing, the app is off, or access is denied here.
        results.append(OrganizationResult(organization_id, name, value))
    return results
