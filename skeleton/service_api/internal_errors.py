"""One classification of the core's internal exceptions (#325).

The REST, the service API and the HTML forms each present an error their own way (status,
``code``, message), but they agree on what the error is: this module is the single place that
decides it. A new internal exception is added here once.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from django.core.exceptions import ValidationError
from django.db import IntegrityError

from skeleton.common.exceptions import (
    ConcurrentUpdate,
    LiveReferencesExist,
    MissingTenantContext,
    QuotaLimitExceeded,
)

Kind = Literal["conflict", "invalid", "context"]


@dataclass(frozen=True, slots=True)
class InternalError:
    """What an internal exception means: a conflict, invalid input or a missing context."""

    kind: Kind
    code: str
    fields: Mapping[str, list[str]] | None = None


def is_unique_conflict(error: ValidationError) -> bool:
    """A model validation error that reports a duplicate (``unique`` or ``unique_together``)."""
    errors = (
        [item for values in error.error_dict.values() for item in values]
        if hasattr(error, "error_dict")
        else error.error_list
    )
    return any(item.code in {"unique", "unique_together"} for item in errors)


def validation_fields(error: ValidationError) -> dict[str, list[str]]:
    return dict(error.message_dict) if hasattr(error, "error_dict") else {"non_field_errors": list(error.messages)}


def classify(exc: BaseException) -> InternalError | None:
    """Classify an internal exception; ``None`` for anything else (authorization included)."""
    # Late imports: access and accounts import the service API package.
    from skeleton.access.services import LastAdministrator
    from skeleton.accounts.platform import LastPlatformAdministrator

    if isinstance(exc, ConcurrentUpdate):
        return InternalError("conflict", "stale_revision")
    if isinstance(exc, LiveReferencesExist):
        return InternalError("conflict", "live_references")
    # Both are ValidationError subclasses: they come before the generic branch.
    if isinstance(exc, LastAdministrator):
        return InternalError("conflict", "last_administrator")
    if isinstance(exc, LastPlatformAdministrator):
        return InternalError("conflict", "last_platform_administrator")
    if isinstance(exc, QuotaLimitExceeded):
        return InternalError("conflict", "quota_exceeded")
    if isinstance(exc, ValidationError):
        if is_unique_conflict(exc):
            return InternalError("conflict", "integrity_conflict", validation_fields(exc))
        return InternalError("invalid", "validation_error", validation_fields(exc))
    if isinstance(exc, IntegrityError):
        return InternalError("conflict", "integrity_conflict")
    if isinstance(exc, MissingTenantContext):
        return InternalError("context", "context_required")
    return None
