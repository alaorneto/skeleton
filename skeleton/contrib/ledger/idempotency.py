"""Scoped idempotency lookup, replay re-authorization, and race handling for the ledger (#888).

An idempotency key never returns an object from another book, source record, or entry kind:

- :func:`find_replay` checks live rows for ``(source_app, idempotency_key)`` in the current
  organization (enforced by RLS). Any existing row whose scope tuple (``book_id``,
  ``source_table``, ``source_id``, ``kind``, ``reverses_id``, ...) differs raises an opaque
  ``Conflict("idempotency_conflict")`` without disclosing any attribute of the existing row.
- Before returning a matched replay, ``authorize`` re-checks read visibility for the caller;
  an unreadable row also raises the same opaque ``Conflict("idempotency_conflict")``.
- :func:`create_or_replay` executes creation inside a savepoint and resolves a concurrent
  uniqueness race through :func:`find_replay`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q

from skeleton.service_api import Conflict
from skeleton.tenancy.context import current_context

Authorizer = Callable[[Any], bool]


def _matches_scope(row: Any, scope: Mapping[str, Any]) -> bool:
    return all(getattr(row, field) == expected for field, expected in scope.items())


def _authorize_replay(row: Any, authorize: Authorizer | None) -> None:
    if authorize is not None and not authorize(row):
        raise Conflict("idempotency_conflict")


def find_replay(
    model: Any,
    *,
    source_app: str,
    key: str,
    scope: Mapping[str, Any],
    authorize: Authorizer | None = None,
) -> Any | None:
    """Return the existing object when ``(source_app, key)`` matches ``scope`` and is readable.

    Raises an opaque ``Conflict("idempotency_conflict")`` when a live row exists for
    ``(source_app, key)`` with a different scope or when the caller cannot read it.
    """
    if not key:
        return None
    rows = list(model.objects.filter(source_app=source_app, idempotency_key=key).order_by("created_at", "sysid")[:2])
    if not rows:
        return None
    matched = [row for row in rows if _matches_scope(row, scope)]
    if len(matched) != 1 or len(rows) > 1:
        raise Conflict("idempotency_conflict")
    row = matched[0]
    _authorize_replay(row, authorize)
    return row


def find_replay_installments(
    model: Any,
    *,
    source_app: str,
    key: str,
    count: int,
    scope: Mapping[str, Any],
    authorize: Authorizer | None = None,
) -> tuple[Any, ...] | None:
    """Return existing installment titles when the whole family matches ``scope`` and ``count``."""
    if not key:
        return None
    rows = list(
        model.objects.filter(
            Q(idempotency_key=key) | Q(idempotency_key__startswith=f"{key}#"),
            source_app=source_app,
        ).order_by("installment_number", "sysid")
    )
    if not rows:
        return None
    expected_keys = [f"{key}#{number}" for number in range(1, count + 1)]
    if len(rows) != count or [row.idempotency_key for row in rows] != expected_keys:
        raise Conflict("idempotency_conflict")
    for row in rows:
        if row.installment_count != count or not _matches_scope(row, scope):
            raise Conflict("idempotency_conflict")
        _authorize_replay(row, authorize)
    return tuple(rows)


def create_or_replay(
    model: Any,
    *,
    source_app: str,
    key: str,
    scope: Mapping[str, Any],
    create: Callable[[], Any],
    authorize: Authorizer | None = None,
    on_conflict: Callable[[], None] | None = None,
) -> Any:
    """Replay an existing match or run ``create`` in a savepoint, resolving races via ``find_replay``."""
    existing = find_replay(model, source_app=source_app, key=key, scope=scope, authorize=authorize)
    if existing is not None:
        return existing
    using = current_context().using
    try:
        with transaction.atomic(using=using):
            return create()
    except (IntegrityError, ValidationError):
        existing = find_replay(model, source_app=source_app, key=key, scope=scope, authorize=authorize)
        if existing is not None:
            return existing
        if on_conflict is not None:
            on_conflict()
        raise


def create_or_replay_installments(
    model: Any,
    *,
    source_app: str,
    key: str,
    count: int,
    scope: Mapping[str, Any],
    create: Callable[[], tuple[Any, ...]],
    authorize: Authorizer | None = None,
) -> tuple[Any, ...]:
    """Replay an existing installment family or create it inside a savepoint."""
    existing = find_replay_installments(
        model, source_app=source_app, key=key, count=count, scope=scope, authorize=authorize
    )
    if existing is not None:
        return existing
    using = current_context().using
    try:
        with transaction.atomic(using=using):
            return create()
    except (IntegrityError, ValidationError):
        existing = find_replay_installments(
            model, source_app=source_app, key=key, count=count, scope=scope, authorize=authorize
        )
        if existing is not None:
            return existing
        raise
